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
const PHASES = {lobby: '等待入座', bidding: '叫牌', discard: '庄家扣牌', playing: '出牌', collecting: '收墩', revealing: '底牌揭晓', finished: '本局结算'};
const POSITIONS = ['bottom', 'right', 'top', 'left'];
let state = null, ws = null, pending = null, selection = [], selectionKey = '', retryTimer;
let connected = false, retryCount = 0, toastTimer, clockOffset = 0, token = '';
let intentionalClose = false;
let leaving = false;
let roomSession = '';
try { roomSession = localStorage.getItem('tarot-session') || ''; } catch (_) {}
try { token = localStorage.getItem('tarot-seat-token') || ''; } catch (_) { /* handled on join */ }
try { intentionalClose = sessionStorage.getItem('tarot-on-home') === '1'; } catch (_) {}

function refreshOccupancy() {
  const requestedToken = token;
  fetch('/api/lobby').then(r => r.json()).then(data => {
    $('occupancy').textContent = `${data.occupied} / 4 已入座`;
    if (data.session_id && roomSession !== data.session_id) {
      const expired = Boolean(requestedToken && requestedToken === token);
      roomSession = data.session_id;
      try { localStorage.setItem('tarot-session', roomSession); } catch (_) {}
      if (expired) forgetSeat();
    }
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
  clearTableEffects();
  closeTablePickers();
  syncMusic();
  awardQueue = [];
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
      roomSession = state.session_id;
      try { localStorage.setItem('tarot-session', roomSession); } catch (_) {}
      const key = `${state.round}:${state.phase}:${state.turn}`;
      if (key !== selectionKey) { selection = []; selectionKey = key; }
      if (state.phase === 'discard' && state.seat === state.taker) selection = [...state.draft];
      else selection = selection.filter(c => state.legal.includes(c));
      render();
    } else if (message.type === 'ack' || message.type === 'error') {
      if (message.id === pending) pending = null;
      if (message.type === 'error') toast(message.message);
      if (state) { renderHand(); renderActions(); renderVote(); renderExtras(); }
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
    const canGift = player && i !== state.seat;
    const avatar = el(canGift ? 'button' : 'div', 'avatar', player ? player.name.slice(0, 1) : '+');
    if (canGift) {
      avatar.type = 'button';
      avatar.setAttribute('aria-label', `给${player.name}送礼物`);
      avatar.setAttribute('aria-haspopup', 'true');
      avatar.addEventListener('click', () => openGiftPicker(i));
    }
    seat.append(avatar);
    const info = el('div');
    const label = el('div', 'seat-name', player ? player.name : '等待好友');
    label.title = player?.name || '等待好友';
    if (player) label.append(el('span', 'seat-role', (i === state.seat ? '你 ' : '') + (i === state.taker && player.played_this_round ? '庄家' : i === state.host ? '房主' : '')));
    const detail = el('div', 'seat-detail');
    if (!player) detail.textContent = '空座位';
    else if (state.phase === 'lobby') detail.textContent = player.online ? '已入座' : '离线 · 保留座位';
    else {
      detail.append(el('span', '', !player.online ? '离线 · ' : state.phase === 'bidding' && state.passed.includes(i) ? '不叫 · ' : `${player.count} 张 · `));
      const history = el('button', 'trick-link', `已收 ${player.tricks} 墩`);
      history.setAttribute('aria-label', `查看${player.name}已收的墩`);
      history.addEventListener('click', () => openInspection('tricks', i));
      detail.append(history);
    }
    info.append(label, detail);
    seat.append(info);
    if (player) seat.dataset.seat = String(i);
    $('seats').append(seat);
  });
}

function renderTable() {
  const center = $('table-center');
  center.replaceChildren();
  $('trick-cards').replaceChildren();
  center.hidden = state.trick.length > 0 || state.phase === 'revealing';
  $('reveal-panel').hidden = state.phase !== 'revealing';
  $('reveal-cards').replaceChildren(...(state.phase === 'revealing' ? state.kitty.map(c => cardElement(c)) : []));
  if (!center.hidden) {
    const occupied = state.players.filter(Boolean).length;
    const online = state.players.filter(p => p?.online).length;
    let title, detail;
    if (state.phase === 'lobby') { title = `${occupied} / 4 已入座`; detail = online === 4 ? '四人到齐，等待房主开始' : '把同一个链接分享给好友'; }
    else if (state.phase === 'bidding') { title = state.contract ? contractName(state.contract) : '叫分，还是不叫？'; detail = `等待 ${name(state.turn)} 叫分`; }
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
  if (!state.hand.length) $('hand').append(el('p', 'empty-hand', ['revealing', 'finished'].includes(state.phase) ? '本局手牌已出完' : '等待发牌，祝你有一手好牌。'));
  $('hand').scrollLeft = scroll;
  $('hand-count').textContent = state.hand.length + ' 张';
  $('my-role').textContent = !state.players[state.seat]?.played_this_round ? '等待开局' : state.taker === null ? '尚未定庄' : state.taker === state.seat ? '庄家 · 一对三' : '防守方';
  $('hand-hint').textContent = !connected ? '正在重连，恢复后即可继续操作。' : isDiscard ? '只能选原手牌；国王、Bout 与非必要将牌不可扣。' :
    state.phase === 'revealing' ? '最后一墩已收完，亮底牌结束后结算。' :
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
    note = state.phase === 'revealing' ? '底牌展示中，结束后自动结算。' : state.turn === null ? '正在收墩…' : `等待 ${name(state.turn)} 操作`;
  } else if (state.phase === 'bidding') {
    note = '叫牌后，你将独自对抗另外三位。';
    $('actions').append(actionButton('不叫', 'bid', {bid: 'pass'}, true, false));
    const highest = state.contracts[state.contract]?.multiplier || 0;
    for (const [key, value] of Object.entries(state.contracts)) {
      if (value.multiplier > highest) $('actions').append(actionButton(contractName(key), 'bid', {bid: key}));
    }
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
  panel.append(stats, el('p', 'result-note', `${r.contract_name} ×${r.multiplier}：（基础 ${signed(r.base)} + 末将 ${signed(r.petit)}）×${r.multiplier} + 大满贯 ${signed(r.slam)} = 每份 ${signed(r.unit)} 分。${r.excuse_note} 庄家愚人补偿净额 ${signed(r.excuse_adjustment)} 分。`));
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
  panel.append(el('p', 'result-note', `${state.discard.length ? '扣牌' : '隐藏底牌'}：${(state.discard.length ? state.discard : state.kitty).map(cardName).join('、')}。六张共 ${r.reserve_points} 分，归${r.reserve_owner === null ? r.reserve_team : participants[r.reserve_owner].name}。两方总牌分 ${r.points + r.defense_points}，四人成绩合计 ${r.scores.reduce((a, b) => a + b, 0)}。`));
}

function renderVote() {
  if (!state) return;
  const active = !['lobby', 'finished'].includes(state.phase);
  const poll = state.restart_poll;
  const voted = poll && [...poll.yes, ...poll.no].includes(state.seat);
  const now = (Date.now() + clockOffset) / 1000;
  const cooldown = Math.max(0, Math.ceil(state.restart_cooldown - now));
  $('restart-panel').hidden = !active;
  $('restart-status').textContent = poll ? `重开剩余 ${Math.max(0, Math.ceil(poll.deadline - now))}s · 同意 ${poll.yes.length}/2（${poll.yes.map(name).join('、') || '无'}）· 拒绝 ${poll.no.length}/3（${poll.no.map(name).join('、') || '无'}）` : cooldown ? `投票未通过，${cooldown}s 后可再次发起。` : '20 秒内两人同意即回准备界面，并清空积分榜。';
  $('restart-button').textContent = voted ? (poll.yes.includes(state.seat) ? '已同意' : '已拒绝') : poll ? '同意重开' : '重开投票';
  $('restart-button').disabled = !connected || Boolean(pending) || leaving || voted || cooldown > 0;
  $('restart-no').hidden = !poll;
  $('restart-no').disabled = !connected || Boolean(pending) || leaving || voted;
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
  $('turn-label').textContent = state.turn === null ? state.phase === 'collecting' ? '收墩展示' : state.phase === 'revealing' ? '结算前亮牌' : '' : state.turn === state.seat ? '轮到你了' : name(state.turn) + ' 的回合';
  $('notice').textContent = state.notice;
  renderSeats(); renderTable(); renderHand(); renderActions(); renderResult(); renderVote();
  renderExtras(); syncMusic(); receiveAwards();
  if (state.phase === 'revealing') {
    closeTablePickers();
    $('inspect-dialog').close();
    $('last-dialog').close();
    $('award-dialog').close();
  }
  if ($('inspect-dialog').open) renderInspection();
  $('contract-label').textContent = state.contract ? contractName(state.contract) : '等待叫分';
  $('kitty-open').hidden = !state.kitty.length;
  $('kitty-open').textContent = state.kitty_revealed ? '查看公开底牌' : '查看结算底牌';
  $('discard-open').hidden = state.seat !== state.taker || !state.discard.length;
  $('kitty-panel').hidden = state.phase !== 'discard';
  $('kitty-cards').replaceChildren(...state.kitty.map(c => cardElement(c)));
  $('kitty-note').textContent = state.seat === state.taker ? '这六张牌不能扣掉，将在扣牌后全部收入。' : '庄家将从原手牌扣六张，再收入这些底牌。';
  $('exposed-trumps').hidden = !state.exposed_trumps.length;
  $('exposed-trumps').textContent = '庄家必要扣下的将牌：' + state.exposed_trumps.map(cardName).join('、');
  updateClock();
}

function updateClock() {
  renderVote();
  if (!state?.deadline) { $('timer').textContent = '—'; $('reveal-timer').textContent = state?.paused ? '已暂停' : '—'; return; }
  const remaining = Math.max(0, Math.ceil((state.deadline * 1000 - Date.now() - clockOffset) / 1000));
  $('timer').textContent = remaining + 's';
  $('reveal-timer').textContent = remaining + 's';
  $('timer').classList.toggle('urgent', remaining <= 8 && ['bidding', 'discard', 'playing'].includes(state.phase));
  if (connected && ['bidding', 'discard', 'playing'].includes(state.phase) && remaining > 0 && remaining <= 5) {
    const key = `${state.round}:${state.deadline}:${remaining}`;
    if (key !== lastBeep) { lastBeep = key; countdownBeep(remaining); }
  }
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
$('restart-button').addEventListener('click', () => send('restart_vote', {round: state.round, poll_id: state.restart_poll?.id || null, choice: 'yes'}));
$('restart-no').addEventListener('click', () => send('restart_vote', {round: state.round, poll_id: state.restart_poll?.id, choice: 'no'}));
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
function contractName(key) {
  const c = state?.contracts[key];
  return c ? `${c.name} ×${c.multiplier}` : '';
}

// Use the supplied recordings; synthesize a short countdown tone locally.
const music = new Audio();
music.loop = true;
music.volume = 0.25;
let audioContext, soundEnabled = true, audioUnlocked = false, currentTrack = '', lastBeep = '';
try { soundEnabled = localStorage.getItem('tarot-sound') !== 'off'; } catch (_) {}
function soundLabel() {
  $('sound-toggle').textContent = !soundEnabled ? '声音已关闭' : !audioUnlocked ? '开启声音' : '声音已开启';
  $('sound-toggle').setAttribute('aria-pressed', String(soundEnabled && audioUnlocked));
}
function syncMusic() {
  const track = !state ? '' : state.phase === 'finished' ? 'jiesuanyinyue' : ['playing', 'collecting', 'revealing'].includes(state.phase) ? 'seikobeatclub' : 'leschampselysee';
  if (track !== currentTrack) {
    music.pause();
    currentTrack = track;
    if (track) { music.src = `/musics/${track}.mp3`; music.load(); }
    else { music.removeAttribute('src'); music.load(); }
  }
  if (!track || !soundEnabled || !audioUnlocked) music.pause();
  else if (music.paused) music.play().then(() => { $('audio-status').textContent = ''; }).catch(error => {
    if (error.name === 'NotAllowedError') { audioUnlocked = false; soundLabel(); }
    else if (error.name !== 'AbortError') $('audio-status').textContent = '音乐暂时无法播放，请检查网络后重新开启声音。';
  });
  soundLabel();
}
function unlockAudio() {
  if (!soundEnabled) return;
  try {
    audioContext ||= new (window.AudioContext || window.webkitAudioContext)();
    audioContext.resume().catch(() => {});
    audioUnlocked = true;
    syncMusic();
  } catch (_) { $('audio-status').textContent = '此浏览器不支持提示音。'; }
}
document.addEventListener('pointerdown', event => { if (!audioUnlocked && !event.target.closest('#sound-toggle')) unlockAudio(); }, {capture: true});
document.addEventListener('keydown', event => { if (!audioUnlocked && !event.target.closest('#sound-toggle')) unlockAudio(); }, {capture: true});
$('sound-toggle').addEventListener('click', () => {
  if (soundEnabled && audioUnlocked) soundEnabled = false;
  else { soundEnabled = true; unlockAudio(); }
  try { localStorage.setItem('tarot-sound', soundEnabled ? 'on' : 'off'); } catch (_) {}
  syncMusic();
});
function countdownBeep(seconds) {
  if (!soundEnabled || !audioContext || audioContext.state !== 'running') return;
  const oscillator = audioContext.createOscillator(), gain = audioContext.createGain();
  oscillator.frequency.value = seconds === 1 ? 1100 : 880;
  oscillator.connect(gain); gain.connect(audioContext.destination);
  const now = audioContext.currentTime;
  gain.gain.setValueAtTime(0, now);
  gain.gain.linearRampToValueAtTime(0.12, now + 0.015);
  gain.gain.exponentialRampToValueAtTime(0.001, now + 0.18);
  oscillator.start(now); oscillator.stop(now + 0.2);
  oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
}

let socialSeen = 0, socialSession = '', giftTarget = null;
const effectTimers = new Set();
function clearTableEffects() {
  effectTimers.forEach(clearTimeout); effectTimers.clear();
  $('table-effects').replaceChildren();
}
function expireEffect(node, milliseconds) {
  const timer = setTimeout(() => { node.remove(); effectTimers.delete(timer); }, milliseconds);
  effectTimers.add(timer);
}
function closeTablePickers() {
  $('gift-picker').hidden = true; $('chat-picker').hidden = true;
  $('chat-open').setAttribute('aria-expanded', 'false');
  giftTarget = null;
}
function openGiftPicker(seat) {
  closeTablePickers();
  if (!state?.players[seat] || seat === state.seat) return;
  giftTarget = seat;
  $('gift-label').textContent = `送给 ${name(seat)}`;
  $('gift-picker').className = `table-picker gift-picker ${pos(seat)}`;
  $('gift-picker').hidden = false;
  $('flower-button').focus({preventScroll: true});
}
function showSocialEvent(event) {
  const remaining = 5000 - (Date.now() + clockOffset - event.time * 1000);
  if (remaining <= 0) return;
  const layer = $('table-effects');
  if (event.kind === 'chat') {
    layer.querySelector(`.speech-bubble[data-speaker="${event.seat}"]`)?.remove();
    const bubble = el('div', `speech-bubble ${pos(event.seat)}`, event.text);
    bubble.dataset.speaker = String(event.seat);
    bubble.setAttribute('aria-label', `${event.name}说：${event.text}`);
    layer.append(bubble);
    expireEffect(bubble, remaining);
    return;
  }
  const from = document.querySelector(`[data-seat="${event.seat}"] .avatar`);
  const to = document.querySelector(`[data-seat="${event.target}"] .avatar`);
  if (!from || !to) return;
  const origin = layer.getBoundingClientRect(), start = from.getBoundingClientRect(), end = to.getBoundingClientRect();
  const flight = el('span', 'gift-flight', event.gift === 'flower' ? '💐' : '🥚');
  flight.setAttribute('aria-label', `${event.name}向${event.target_name}${event.gift === 'flower' ? '送鲜花' : '投鸡蛋'}`);
  layer.append(flight);
  const startX = start.x + start.width / 2 - origin.x - 22, startY = start.y + start.height / 2 - origin.y - 22;
  const endX = end.x + end.width / 2 - origin.x - 22, endY = end.y + end.height / 2 - origin.y - 22;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const destination = `translate(${endX}px, ${endY}px)`;
  if (event.gift === 'egg') {
    const flightTime = reduced ? 0 : Math.min(650, remaining * .35);
    const impactTime = Math.min(1750, remaining - flightTime);
    const impact = () => {
      if (!flight.isConnected) return;
      flight.classList.add('egg-impact');
      flight.setAttribute('aria-label', `${event.name}投出的鸡蛋在${event.target_name}头像上破碎`);
      flight.replaceChildren(...['egg-white', 'egg-yolk', 'egg-shell shell-left', 'egg-shell shell-right',
        'egg-drop drop-one', 'egg-drop drop-two', 'egg-drop drop-three'].map(className => {
        const part = el('span', className); part.setAttribute('aria-hidden', 'true'); return part;
      }));
      flight.animate(reduced ? [
        {transform: destination, opacity: 1, offset: 0},
        {transform: destination, opacity: 1, offset: .8},
        {transform: destination, opacity: 0, offset: 1}
      ] : [
        {transform: `${destination} scale(.4)`, opacity: 1, offset: 0},
        {transform: `${destination} scale(1.2)`, opacity: 1, offset: .12},
        {transform: `${destination} scale(1)`, opacity: 1, offset: .25},
        {transform: `${destination} scale(1)`, opacity: 1, offset: .8},
        {transform: `${destination} scale(1)`, opacity: 0, offset: 1}
      ], {duration: impactTime, easing: 'ease-out', fill: 'forwards'});
      expireEffect(flight, impactTime);
    };
    if (reduced) impact();
    else {
      const animation = flight.animate([
        {transform: `translate(${startX}px, ${startY}px) scale(.7)`, offset: 0},
        {transform: `translate(${(startX + endX) / 2}px, ${(startY + endY) / 2 - 50}px) rotate(-25deg)`, offset: .5},
        {transform: destination, offset: 1}
      ], {duration: flightTime, easing: 'ease-in', fill: 'forwards'});
      animation.finished.then(impact).catch(() => flight.remove());
    }
    return;
  }
  flight.animate(reduced ? [{transform: destination, opacity: 1}, {transform: destination, opacity: 0}] : [
    {transform: `translate(${startX}px, ${startY}px) scale(.7)`, opacity: 1, offset: 0},
    {transform: `translate(${(startX + endX) / 2}px, ${(startY + endY) / 2 - 50}px) rotate(-25deg)`, opacity: 1, offset: .25},
    {transform: `${destination} scale(1.3)`, opacity: 1, offset: .5},
    {transform: `${destination} scale(1)`, opacity: 1, offset: .8},
    {transform: `${destination} scale(1.1)`, opacity: 0, offset: 1}
  ], {duration: Math.min(2400, remaining), easing: 'ease-out', fill: 'forwards'});
  expireEffect(flight, Math.min(2400, remaining));
}
function renderExtras() {
  $('scoreboard').replaceChildren();
  [...state.players].filter(Boolean).sort((a, b) => b.total - a.total).forEach((p, rank) => {
    const row = el('div', 'score-row');
    row.append(el('span', '', `${rank + 1}. ${p.name}`), el('b', '', signed(p.total)));
    $('scoreboard').append(row);
  });
  $('quick-chat').replaceChildren(...state.quick_chats.map(text => {
    const button = actionButton(text, 'chat', {text}, true, false);
    button.addEventListener('click', closeTablePickers);
    return button;
  }));
  if (giftTarget !== null && !state.players[giftTarget]) closeTablePickers();
  for (const id of ['flower-button', 'egg-button']) $(id).disabled = !connected || Boolean(pending) || leaving;
  $('chat-open').disabled = !connected || leaving;
  if (socialSession !== state.session_id) { socialSeen = 0; socialSession = state.session_id; clearTableEffects(); }
  for (const event of state.social_events) {
    if (event.id <= socialSeen) continue;
    socialSeen = event.id;
    showSocialEvent(event);
  }
}
for (const [id, gift] of [['flower-button', 'flower'], ['egg-button', 'egg']]) {
  $(id).addEventListener('click', () => { if (giftTarget !== null) send('gift', {target: giftTarget, gift}); closeTablePickers(); });
}
$('chat-open').addEventListener('click', () => {
  const opening = $('chat-picker').hidden;
  closeTablePickers(); $('chat-picker').hidden = !opening;
  $('chat-open').setAttribute('aria-expanded', String(opening));
  if (opening) $('quick-chat').querySelector('button')?.focus({preventScroll: true});
});
$('chat-close').addEventListener('click', closeTablePickers);
$('gift-close').addEventListener('click', closeTablePickers);
document.addEventListener('click', event => {
  if (!event.target.closest('.table-picker, .avatar, #chat-open')) closeTablePickers();
});
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeTablePickers(); });

let inspectMode = 'tricks', inspectSeat = 0;
function renderInspection() {
  $('inspect-tabs').replaceChildren();
  const content = $('inspect-content'); content.replaceChildren();
  const previousName = i => state.result?.players?.[i]?.name || name(i);
  if (inspectMode !== 'tricks') {
    $('inspect-title').textContent = inspectMode === 'kitty' ? '本局底牌' : '我的扣牌';
    const cards = inspectMode === 'kitty' ? state.kitty : state.discard;
    const row = el('div', 'mini-hand'); row.append(...cards.map(c => cardElement(c)));
    content.append(row, el('p', 'result-note', cards.length ? '本局记录，随时可回看。' : '当前没有可查看的牌。'));
    return;
  }
  $('inspect-title').textContent = '查看已收的墩';
  state.players.forEach((p, i) => {
    if (!p) return;
    const button = el('button', 'button quiet', previousName(i));
    button.setAttribute('aria-pressed', String(inspectSeat === i));
    button.addEventListener('click', () => { inspectSeat = i; renderInspection(); });
    $('inspect-tabs').append(button);
  });
  state.tricks.forEach((trick, index) => {
    if (trick.winner !== inspectSeat) return;
    const section = el('section', 'history-trick');
    section.append(el('h3', '', `第 ${index + 1} 墩 · ${previousName(inspectSeat)} 收墩`));
    const row = el('div', 'mini-hand');
    for (const play of trick.plays) {
      if (play.card === 'F' && play.seat !== trick.winner) continue;
      const item = el('div'); item.append(cardElement(play.card), el('small', '', previousName(play.seat))); row.append(item);
    }
    section.append(row);
    if (trick.kept_fool !== null && trick.kept_fool !== undefined) section.append(el('p', 'result-note', `愚人由 ${previousName(trick.kept_fool)} 保留，并补 0.5 分（同阵营内部补牌不改变阵营总分）。`));
    content.append(section);
  });
  const ownFool = state.tricks.find(t => t.kept_fool === inspectSeat);
  if (ownFool) { const section = el('section', 'history-trick'); section.append(el('h3', '', '保留的愚人'), cardElement('F')); content.append(section); }
  if (state.result?.contract === 'jiedi' && state.result.reserve_owner === inspectSeat) {
    const section = el('section', 'history-trick');
    const cards = el('div', 'mini-hand'); cards.append(...state.discard.map(c => cardElement(c)));
    section.append(el('h3', '', 'Jiedi · 末墩收下的扣牌'), cards); content.append(section);
  }
  if (!content.children.length) content.append(el('p', 'result-note', '这位玩家尚未收墩。'));
}
function openInspection(mode, seat = state.seat) {
  inspectMode = mode; inspectSeat = seat; renderInspection(); $('inspect-dialog').showModal();
}
for (const [id, mode] of [['kitty-open', 'kitty'], ['discard-open', 'discard']]) {
  $(id).addEventListener('click', () => openInspection(mode));
}

const AWARDS = {
  streak: ['😰', 'TA的连胜，我很怕', '连续三场游戏获胜，播报后重新计数。'],
  taker: ['😡', '不要，妨碍我', '两次成为庄家并获胜；当庄失败或播报后重新计数。'],
  turtle: ['🐢', '玄武制胜', '连续五次发牌都不叫分。拿出勇气来！'],
  bidder: ['🙋', '我叫，我再叫！', '连续三次发牌叫分，播报后重新计数。']
};
let awardQueue = [], awardSeen = 0, awardSession = '';
try { const saved = JSON.parse(sessionStorage.getItem('tarot-awards') || '{}'); awardSeen = saved.seen || 0; awardSession = saved.session || ''; } catch (_) {}
function receiveAwards() {
  // A room reset can reuse round numbers, so announcements use their own epoch.
  const session = `${state.session_id}:${state.award_epoch}`;
  if (awardSession !== session) { awardSeen = 0; awardQueue = []; awardSession = session; $('award-dialog').close(); }
  for (const event of state.announcements) {
    if (event.id > awardSeen) { awardSeen = event.id; awardQueue.push(event); }
  }
  try { sessionStorage.setItem('tarot-awards', JSON.stringify({session: awardSession, seen: awardSeen})); } catch (_) {}
  showNextAward();
}
function showNextAward() {
  if ($('award-dialog').open || !awardQueue.length || !state || state.phase === 'revealing') return;
  const event = awardQueue.shift(), definition = AWARDS[event.kind];
  if (!definition) return;
  $('award-emoji').textContent = definition[0]; $('award-title').textContent = definition[1];
  $('award-names').textContent = event.names.join('、'); $('award-detail').textContent = definition[2];
  $('award-dialog').showModal();
}
$('award-close').addEventListener('click', () => $('award-dialog').close());
$('award-dialog').addEventListener('close', () => { if (state) showNextAward(); });
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
