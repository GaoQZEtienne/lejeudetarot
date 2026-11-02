"""Single-process, authoritative four-seat tarot room."""
import argparse
import asyncio
from contextlib import suppress
from copy import deepcopy
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from game.rules import RuleError, discard_options, lead_suit, legal_plays
from game.state import Game
from game.storage import Storage

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('tarot')
OUTSIDE_PHASES = ('lobby', 'finished')


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Room:
    def __init__(self, storage, durations=None):
        self.storage = storage
        saved = storage.load() or {}
        self.game = Game(saved.get('game'))
        self.players = saved.get('players', [None] * 4)
        self.host = saved.get('host', None)
        self.revision = saved.get('revision', 0)
        self.durations = dict(bidding=30, discard=90, playing=30, collecting=2)
        self.durations.update(durations or {})
        self.connections = [set() for _ in range(4)]
        self.close_tasks = set()
        self.offline_since = [time.time()] * 4
        self.deadline = None
        self.lock = asyncio.Lock()
        self.paused = True

    def snapshot(self):
        return dict(game=deepcopy(self.game.data), players=deepcopy(self.players), host=self.host,
                    revision=self.revision)

    def save(self):
        self.storage.save(self.snapshot())

    def online(self, seat):
        return bool(self.connections[seat])

    def set_deadline(self):
        duration = self.durations.get(self.game.data['phase'])
        self.deadline = time.time() + duration if duration is not None and not self.paused else None

    def transaction(self, fn, reset_clock=True):
        before = self.snapshot()
        old_deadline = self.deadline
        try:
            old_phase = self.game.data['phase']
            fn()
            if old_phase != 'finished' and self.game.data['phase'] == 'finished':
                for player, score in zip(self.players, self.game.data['result']['scores']):
                    player['total2'] += round(score * 2)
                self.freeze_result_players()
                self.game.data['restart_votes'] = []
            self.revision += 1
            if reset_clock:
                self.set_deadline()
            self.save()
        except Exception:
            self.game = Game(before['game'])
            self.players, self.host = before['players'], before['host']
            self.revision, self.deadline = before['revision'], old_deadline
            raise

    def identify(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            return None
        digest = token_hash(token)
        return next((i for i, p in enumerate(self.players) if p and secrets.compare_digest(p['token_hash'], digest)), None)

    def freeze_result_players(self):
        result = self.game.data.get('result')
        if result and 'players' not in result:
            result['players'] = [dict(name=p['name'], total=p['total2'] / 2) for p in self.players]

    def release_seat(self, seat):
        if self.game.data['phase'] not in OUTSIDE_PHASES:
            return False

        def apply():
            self.freeze_result_players()
            self.players[seat] = None
            if self.host == seat:
                remaining = [i for i, p in enumerate(self.players) if p]
                self.host = next((i for i in remaining if self.online(i)), next(iter(remaining), None))
            if not any(self.players):
                self.game = Game()
        self.transaction(apply, reset_clock=False)
        return True

    def join(self, name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 16:
            raise RuleError('昵称请输入 1～16 个字符')
        name = name.strip()
        if any(ord(c) < 32 for c in name):
            raise RuleError('昵称不能包含控制字符')
        if self.game.data['phase'] not in OUTSIDE_PHASES or all(self.players):
            raise RuleError('四个座位已保留，请使用原浏览器返回房间')
        if any(p and p['name'] == name for p in self.players):
            raise RuleError('这个昵称已经入座，请换一个昵称')
        seat = self.players.index(None)
        token = secrets.token_urlsafe(32)

        def apply():
            self.freeze_result_players()
            self.players[seat] = dict(name=name, token_hash=token_hash(token), total2=0,
                                      joined_round=self.game.data['round'])
            if self.host is None:
                self.host = seat
        self.transaction(apply, reset_clock=False)
        return seat, token

    def view(self, seat):
        s = self.game.data
        hand = list(s['hands'][seat])
        legal, reasons = [], {}
        if seat == s['turn'] and s['phase'] == 'playing':
            legal = legal_plays(hand, s['trick'])
        elif seat == s['turn'] and s['phase'] == 'discard':
            plain, trumps = discard_options(hand)
            legal = plain + trumps
        for c in hand:
            if c not in legal:
                reasons[c] = ('国王、Bout 和非必要的将牌不能扣' if s['phase'] == 'discard' and seat == s['turn']
                              else '须跟首攻花色，缺门时须出将' if seat == s['turn'] and s['phase'] == 'playing'
                              else '等待轮到你操作')
        players = []
        for i, p in enumerate(self.players):
            played = p is not None and p.get('joined_round', -1) < s['round']
            players.append(None if not p else dict(name=p['name'], seat=i, online=self.online(i),
                count=len(s['hands'][i]), total=p['total2'] / 2,
                played_this_round=played,
                tricks=sum(t['winner'] == i for t in s['tricks']) if played else 0))
        public_kitty = s['kitty'] if s['phase'] not in ('lobby', 'bidding') else []
        return dict(type='state', revision=self.revision, seat=seat, host=self.host, players=players,
            phase=s['phase'], round=s['round'], turn=s['turn'], taker=s['taker'], first=s['first'],
            passed=list(s['passed']), hand=hand, legal=legal, reasons=reasons, kitty=public_kitty,
            exposed_trumps=list(s['exposed_trumps']), trick=deepcopy(s['trick']),
            last_trick=deepcopy(s['tricks'][-1]) if s['tricks'] else None,
            trick_number=min(18, len(s['tricks']) + 1), completed=len(s['tricks']),
            lead=lead_suit(s['trick']), draft=list(s['draft']) if seat == s['taker'] else [],
            discard=list(s['discard']) if s['phase'] == 'finished' else [],
            result=deepcopy(s['result']), notice=s['notice'], deadline=self.deadline,
            server_time=time.time(), paused=self.paused,
            restart_votes=list(s.get('restart_votes', [])),
            duration=self.durations.get(s['phase'], 0))

    async def send(self, ws, data):
        try:
            await asyncio.wait_for(ws.send_json(data), timeout=2)
        except (ConnectionError, RuntimeError, asyncio.TimeoutError):
            # A close handshake can wait for its reader, whose finally block needs
            # the room lock. Never wait for that handshake while broadcasting
            # under this lock (simultaneous disconnects would otherwise stall).
            task = asyncio.create_task(ws.close())
            self.close_tasks.add(task)
            task.add_done_callback(self.close_tasks.discard)

    async def broadcast(self):
        await asyncio.gather(*(self.send(ws, self.view(i)) for i in range(4)
                               for ws in tuple(self.connections[i])), return_exceptions=True)

    async def disconnect(self, seat, ws):
        async with self.lock:
            if ws in self.connections[seat]:
                self.connections[seat].discard(ws)
                if not self.online(seat):
                    self.offline_since[seat] = time.time()
                if not any(self.connections):
                    self.paused = True
                    self.deadline = None
                await self.broadcast()

    def advance_if_due(self):
        if self.deadline is None or time.time() < self.deadline:
            return False
        phase = self.game.data['phase']
        def apply():
            self.game.timeout()
            if phase != 'collecting':
                self.game.data['notice'] = '上一操作超时，系统已自动处理。' + self.game.data['notice']
        self.transaction(apply)
        return True

    def handle_action(self, seat, data):
        action = data.get('action')
        payload = data.get('payload', {})
        if not isinstance(payload, dict):
            raise RuleError('操作格式不正确')
        if action == 'restart_vote':
            s = self.game.data
            if s['phase'] in OUTSIDE_PHASES:
                raise RuleError('只有局内可以发起重开投票')
            if payload.get('round') != s['round']:
                raise RuleError('已经重新发牌，上一局的投票已失效')
            votes = s.get('restart_votes', [])
            if seat in votes:
                return

            def vote():
                s.setdefault('restart_votes', []).append(seat)
                if len(s['restart_votes']) >= 2:
                    self.game.new_deal('两名玩家赞成重开，原局不计分，重新发牌。')
            # Votes may arrive together or between plays. Scope them to this
            # deal, rather than rejecting the second voter for an older revision.
            self.transaction(vote, reset_clock=len(votes) >= 1)
            return
        if data.get('revision') != self.revision:
            raise RuleError('牌局状态已更新，请按最新画面重新操作')
        if action == 'start':
            if seat != self.host:
                raise RuleError('只有房主可以开始')
            if self.game.data['phase'] not in ('lobby', 'finished'):
                raise RuleError('当前牌局尚未结束')
            if not all(self.players) or not all(self.online(i) for i in range(4)):
                raise RuleError('请等待四位玩家全部在线')
            self.transaction(self.game.new_deal)
        else:
            self.transaction(lambda: self.game.action(seat, action, payload), reset_clock=action != 'draft')

    async def tick(self):
        while True:
            await asyncio.sleep(0.2)
            async with self.lock:
                changed = False
                if self.game.data['phase'] in ('lobby', 'finished') and self.host is not None:
                    if not self.online(self.host) and time.time() - self.offline_since[self.host] >= 30:
                        candidate = next((i for i in range(4) if self.online(i)), None)
                        if candidate is not None:
                            self.transaction(lambda: setattr(self, 'host', candidate), reset_clock=False)
                            changed = True
                if not self.paused:
                    changed = self.advance_if_due() or changed
                if changed:
                    await self.broadcast()


ROOM_KEY = web.AppKey('room', Room)


@web.middleware
async def guard(request, handler):
    origin = request.headers.get('Origin')
    if origin and urlsplit(origin).netloc != request.host:
        return web.json_response({'error': '请从本房间网页发起操作'}, status=403)
    try:
        response = await handler(request)
    except RuleError as exc:
        return web.json_response({'error': str(exc)}, status=400)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = "default-src 'self'; connect-src 'self' ws: wss:; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
    return response


async def index(request):
    return web.FileResponse(ROOT / 'static' / 'index.html')


async def health(request):
    return web.json_response({'ok': True})


async def lobby(request):
    room = request.app[ROOM_KEY]
    return web.json_response({'occupied': sum(p is not None for p in room.players)})


async def join(request):
    room = request.app[ROOM_KEY]
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuleError('请求格式不正确')
    if not isinstance(data, dict):
        raise RuleError('请求格式不正确')
    async with room.lock:
        seat, token = room.join(data.get('name'))
        await room.broadcast()
    return web.json_response({'seat': seat, 'token': token})


async def leave(request):
    room = request.app[ROOM_KEY]
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuleError('请求格式不正确')
    if not isinstance(data, dict):
        raise RuleError('请求格式不正确')
    departing = []
    async with room.lock:
        seat = room.identify(data.get('token'))
        if seat is None:
            return web.json_response({'released': True})
        if not room.paused:
            room.advance_if_due()
        released = room.release_seat(seat)
        if released:
            # Revoke every tab of this identity before making the seat available.
            departing = tuple(room.connections[seat])
            room.connections[seat].clear()
            if not any(room.connections):
                room.paused, room.deadline = True, None
            await asyncio.gather(*(room.send(ws, {'type': 'released'}) for ws in departing))
            await room.broadcast()
    await asyncio.gather(*(ws.close(code=4002, message=b'Seat released') for ws in departing))
    return web.json_response({'released': released})


async def websocket(request):
    room = request.app[ROOM_KEY]
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=8192)
    await ws.prepare(request)
    seat = None
    try:
        try:
            auth = await ws.receive_json(timeout=10)
        except (ValueError, TypeError, asyncio.TimeoutError):
            await ws.close(code=4001, message=b'Authentication required')
            return ws
        seat = room.identify(auth.get('token')) if isinstance(auth, dict) else None
        if seat is None:
            await room.send(ws, {'type': 'auth_error', 'message': '座位凭据无效，请使用原浏览器或重新入座。'})
            await ws.close(code=4001)
            return ws
        async with room.lock:
            if room.identify(auth.get('token')) != seat:
                await room.send(ws, {'type': 'released'})
                return ws
            room.connections[seat].add(ws)
            if room.paused:
                room.paused = False
                room.set_deadline()
            await room.broadcast()
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            async with room.lock:
                data = {}
                try:
                    if room.identify(auth.get('token')) != seat or ws not in room.connections[seat]:
                        await room.send(ws, {'type': 'released'})
                        break
                    data = json.loads(msg.data)
                    if not isinstance(data, dict):
                        raise RuleError('操作格式不正确')
                    room.advance_if_due()
                    room.handle_action(seat, data)
                    await room.broadcast()
                    await room.send(ws, {'type': 'ack', 'id': data.get('id')})
                except (RuleError, json.JSONDecodeError) as exc:
                    await room.send(ws, {'type': 'error', 'id': data.get('id') if isinstance(data, dict) else None,
                                         'message': str(exc)})
                    await room.broadcast()
    except (ConnectionError, asyncio.TimeoutError):
        pass
    finally:
        if seat is not None:
            # Browser teardown can cancel the HTTP handler while it waits for
            # the room lock. Presence cleanup must survive that cancellation.
            task = asyncio.create_task(room.disconnect(seat, ws))
            room.close_tasks.add(task)
            task.add_done_callback(room.close_tasks.discard)
            await asyncio.shield(task)
    return ws


async def lifecycle(app):
    room = app[ROOM_KEY]
    task = asyncio.create_task(room.tick())
    def report_failure(done):
        if not done.cancelled() and done.exception():
            exc = done.exception()
            LOG.error('Timer stopped; inspect server error before continuing.', exc_info=(type(exc), exc, exc.__traceback__))
    task.add_done_callback(report_failure)
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    room.storage.close()


async def shutdown(app):
    # Close live sockets before aiohttp waits for request handlers to finish.
    room = app[ROOM_KEY]
    sockets = [ws for connections in room.connections for ws in tuple(connections)]
    await asyncio.gather(*(ws.close(code=1001, message=b'Server shutdown') for ws in sockets))
    if room.close_tasks:
        await asyncio.gather(*tuple(room.close_tasks), return_exceptions=True)


def create_app(db_path=None, durations=None):
    app = web.Application(middlewares=[guard], client_max_size=8192)
    app[ROOM_KEY] = Room(Storage(db_path or os.environ.get('TAROT_DB', str(ROOT / 'data' / 'room.sqlite3'))), durations)
    app.add_routes([web.get('/', index), web.get('/health', health), web.get('/api/lobby', lobby),
                    web.post('/api/join', join), web.post('/api/leave', leave), web.get('/ws', websocket)])
    # Expose only the three frontend files, never the database or source tree.
    for filename in ('app.js', 'styles.css'):
        async def asset(request, name=filename):
            return web.FileResponse(ROOT / 'static' / name)
        app.router.add_get('/' + filename, asset)
    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(shutdown)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='四人法式塔罗牌')
    parser.add_argument('--host', default=os.environ.get('HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8080')))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(), host=args.host, port=args.port, access_log=None)
