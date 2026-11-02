import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from game.rules import auto_discard
from game.storage import Storage
from server import ROOM_KEY, Room, create_app


class Peer:
    def __init__(self, ws):
        self.ws, self.state = ws, None
        self.condition = asyncio.Condition()
        self.responses = asyncio.Queue()
        self.task = asyncio.create_task(self.read())
        self.serial = 0

    async def read(self):
        async for msg in self.ws:
            data = json.loads(msg.data)
            if data['type'] == 'state':
                async with self.condition:
                    self.state = data
                    self.condition.notify_all()
            else:
                await self.responses.put(data)

    async def wait(self, predicate):
        async with self.condition:
            await asyncio.wait_for(self.condition.wait_for(lambda: self.state is not None and predicate(self.state)), 5)
        return self.state

    async def act(self, action, payload=None, revision=None):
        self.serial += 1
        await self.ws.send_json({'id': self.serial, 'action': action, 'payload': payload or {},
                                 'revision': self.state['revision'] if revision is None else revision})
        return await asyncio.wait_for(self.responses.get(), 5)

    async def close(self):
        await self.ws.close()
        await self.task


class RoomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'room.sqlite3')
        self.app = create_app(self.db, {'collecting': 0.01})
        self.room = self.app[ROOM_KEY]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.peers, self.tokens = [], []
        for i in range(4):
            response = await self.client.post('/api/join', json={'name': f'玩家{i + 1}'})
            self.assertEqual(response.status, 200)
            token = (await response.json())['token']
            self.tokens.append(token)
            self.peers.append(await self.connect(token))
        await self.sync()

    async def connect(self, token):
        ws = await self.client.ws_connect('/ws')
        await ws.send_json({'token': token})
        peer = Peer(ws)
        await peer.wait(lambda s: True)
        return peer

    async def sync(self):
        revision = self.room.revision
        await asyncio.gather(*(p.wait(lambda s: s['revision'] == revision) for p in self.peers))

    async def asyncTearDown(self):
        await asyncio.gather(*(p.close() for p in self.peers))
        await self.client.close()
        self.tmp.cleanup()

    async def act(self, seat, action, payload=None):
        await self.sync()
        answer = await self.peers[seat].act(action, payload)
        self.assertEqual(answer['type'], 'ack', answer)
        await self.sync()

    async def start(self):
        await self.act(self.room.host, 'start')

    async def take(self):
        await self.act(self.room.game.data['turn'], 'bid', {'bid': 'take'})
        while self.room.game.data['phase'] == 'bidding':
            await self.act(self.room.game.data['turn'], 'bid', {'bid': 'pass'})
        taker = self.room.game.data['taker']
        choice = auto_discard(self.peers[taker].state['hand'])
        await self.act(taker, 'discard', {'cards': choice})

    async def test_four_clients_complete_round_reconnect_and_privacy(self):
        self.assertEqual((await self.client.get('/')).status, 200)
        self.assertEqual((await self.client.get('/data/room.sqlite3')).status, 404)
        fifth = await self.client.post('/api/join', json={'name': '第五位'})
        self.assertEqual(fifth.status, 400)
        self.assertEqual((await self.peers[1].act('start'))['type'], 'error')
        await self.start()
        for peer in self.peers:
            self.assertEqual(len(peer.state['hand']), 18)
            self.assertEqual(peer.state['kitty'], [])
            self.assertNotIn('hands', peer.state)
            self.assertNotIn('token_hash', json.dumps(peer.state))
        self.assertEqual(len(set(sum((p.state['hand'] for p in self.peers), []))), 72)
        for _ in range(4):
            await self.act(self.room.game.data['turn'], 'bid', {'bid': 'pass'})
        self.assertEqual(self.room.game.data['round'], 2)
        await self.take()
        seat = self.room.game.data['turn']
        old_hand = self.peers[seat].state['hand']
        old_deadline = self.room.deadline
        await self.peers[seat].close()
        self.peers[seat] = await self.connect(self.tokens[seat])
        self.assertEqual(self.peers[seat].state['hand'], old_hand)
        self.assertEqual(self.room.deadline, old_deadline)
        await self.sync()
        # Replay an old revision: it must not consume a card.
        result = await self.peers[seat].act('play', {'card': old_hand[0]}, revision=-1)
        self.assertEqual(result['type'], 'error')
        self.assertEqual(self.room.game.data['hands'][seat], old_hand)
        while self.room.game.data['phase'] != 'finished':
            if self.room.game.data['phase'] == 'collecting':
                await self.peers[0].wait(lambda s: s['phase'] != 'collecting')
                await self.sync()
                continue
            seat = self.room.game.data['turn']
            await self.sync()
            await self.act(seat, 'play', {'card': self.peers[seat].state['legal'][0]})
        await self.sync()
        result = self.peers[0].state['result']
        self.assertEqual(self.peers[0].state['completed'], 18)
        self.assertEqual(result['points'] + result['defense_points'], 91)
        self.assertEqual(sum(result['scores']), 0)
        for peer in self.peers:
            self.assertEqual(peer.state['result'], result)
            self.assertEqual(peer.state['hand'], [])
        saved = self.room.storage.load()
        self.assertEqual([p['total2'] / 2 for p in saved['players']], result['scores'])

    async def test_timer_uses_legal_card_and_preserves_discard_draft(self):
        await self.start()
        taker = self.room.game.data['turn']
        await self.act(taker, 'bid', {'bid': 'take'})
        while self.room.game.data['phase'] == 'bidding':
            await self.act(self.room.game.data['turn'], 'bid', {'bid': 'pass'})
        selected = auto_discard(self.peers[taker].state['hand'])[:3]
        deadline = self.room.deadline
        await self.act(taker, 'draft', {'cards': selected})
        self.assertEqual(self.room.deadline, deadline)
        for i, peer in enumerate(self.peers):
            self.assertEqual(peer.state['draft'], selected if i == taker else [])
        async with self.room.lock:
            self.room.deadline = time.time() - 1
        await self.peers[0].wait(lambda s: s['phase'] == 'playing')
        self.assertTrue(set(selected) <= set(self.room.game.data['discard']))
        await self.sync()
        seat = self.room.game.data['turn']
        legal = self.peers[seat].state['legal']
        async with self.room.lock:
            self.room.deadline = time.time() - 1
        await self.peers[0].wait(lambda s: len(s['trick']) == 1)
        self.assertIn(self.room.game.data['trick'][0]['card'], legal)

    async def test_duplicate_connection_pause_and_service_restart_clears_room(self):
        await self.start()
        await self.take()
        twin = await self.connect(self.tokens[0])
        self.assertEqual(twin.state['seat'], 0)
        await twin.close()
        self.assertTrue(self.room.online(0))
        saved_hand = list(self.room.game.data['hands'][0])
        for peer in self.peers:
            await peer.close()
        self.assertTrue(self.room.paused)
        self.assertIsNone(self.room.deadline)
        # Reconnecting a tab preserves hands, but restarting the service clears all identities.
        restored_storage = Storage(self.db)
        restored = Room(restored_storage)
        self.assertEqual(restored.game.data['hands'][0], [])
        self.assertIsNone(restored.identify(self.tokens[0]))
        self.assertEqual(restored.players, [None] * 4)
        self.assertEqual(restored.game.data['phase'], 'lobby')
        self.assertTrue(restored.paused)
        self.assertIsNone(restored.deadline)
        restored_storage.close()
        self.peers = [await self.connect(token) for token in self.tokens]
        self.assertEqual(self.peers[0].state['hand'], saved_hand)
        self.assertFalse(self.room.paused)
        self.assertGreater(self.room.deadline, time.time())

    async def test_cancelled_handlers_still_release_presence(self):
        await self.start()
        # Abrupt browser teardown may cancel a handler again while its finally
        # block is waiting for another player's broadcast to release the lock.
        handlers = [next(iter(group))._req.task for group in self.room.connections]
        async with self.room.lock:
            for task in handlers:
                task.cancel()
            await asyncio.sleep(0.01)
            for task in handlers:
                task.cancel()
            await asyncio.sleep(0.01)
        async with asyncio.timeout(2):
            while not self.room.paused:
                await asyncio.sleep(0.01)
        self.assertFalse(any(self.room.connections))
        self.assertIsNone(self.room.deadline)
        self.assertTrue(all(self.room.players))

    async def test_failed_send_does_not_wait_for_close_under_room_lock(self):
        room = self.room
        class ClosingSocket:
            closed = False

            async def send_json(self, data):
                raise ConnectionError('reader disconnected')

            async def close(self):
                async with room.lock:
                    self.closed = True

        socket = ClosingSocket()
        async with room.lock:
            await asyncio.wait_for(room.send(socket, {}), 0.5)
            closing = tuple(room.close_tasks)
        await asyncio.wait_for(asyncio.gather(*closing), 0.5)
        self.assertTrue(socket.closed)

    async def test_lobby_leave_releases_identity_all_tabs_and_host(self):
        twin = await self.connect(self.tokens[0])
        original_token = self.tokens[0]
        response = await self.client.post('/api/leave', json={'token': original_token})
        self.assertEqual(await response.json(), {'released': True})
        self.assertEqual((await (await self.client.get('/api/lobby')).json())['occupied'], 3)
        self.assertIsNone(self.room.identify(original_token))
        self.assertEqual(self.room.host, 1)
        self.assertFalse(self.room.online(0))
        await twin.close()
        new = await (await self.client.post('/api/join', json={'name': '新玩家'})).json()
        self.assertEqual(new['seat'], 0)
        self.tokens[0] = new['token']
        self.peers[0] = await self.connect(new['token'])
        self.assertEqual(self.peers[0].state['players'][0]['name'], '新玩家')
        # A late retry with a departed identity cannot evict the replacement.
        await self.client.post('/api/leave', json={'token': original_token})
        self.assertEqual(self.room.identify(new['token']), 0)
        self.assertTrue(self.room.online(0))
        old = await self.client.ws_connect('/ws')
        await old.send_json({'token': original_token})
        self.assertEqual((await old.receive_json())['type'], 'auth_error')
        await old.close()

    async def test_ingame_leave_keeps_seat_and_hand(self):
        await self.start()
        await self.take()
        before = list(self.room.game.data['hands'][0])
        deadline = self.room.deadline
        response = await self.client.post('/api/leave', json={'token': self.tokens[0]})
        self.assertEqual(await response.json(), {'released': False})
        await self.peers[0].close()
        self.assertEqual((await (await self.client.get('/api/lobby')).json())['occupied'], 4)
        self.assertEqual((await self.client.post('/api/join', json={'name': '不能插入'})).status, 400)
        self.peers[0] = await self.connect(self.tokens[0])
        self.assertEqual(self.peers[0].state['hand'], before)
        self.assertEqual(self.room.deadline, deadline)

    async def test_restart_requires_two_people_without_resetting_first_vote_timer(self):
        await self.start()
        await self.take()
        round_number = self.room.game.data['round']
        deadline = self.room.deadline
        original_revision = self.room.revision
        self.room.players[0]['total2'] = 300
        self.assertEqual((await self.peers[0].act('restart_vote', {'round': round_number}))['type'], 'ack')
        self.assertEqual(self.room.deadline, deadline)
        poll = self.room.game.data['restart_poll']
        self.assertEqual(poll['yes'], [0])
        self.assertAlmostEqual(poll['deadline'] - time.time(), 20, delta=1)
        ballot = {'round': round_number, 'poll_id': poll['id']}
        # Two tabs of one player still provide only one vote.
        twin = await self.connect(self.tokens[0])
        self.assertEqual((await twin.act('restart_vote', ballot))['type'], 'ack')
        await twin.close()
        self.assertEqual(self.room.game.data['round'], round_number)
        self.assertEqual(self.room.game.data['restart_poll']['yes'], [0])
        saved = self.room.storage.load()
        self.assertEqual(saved['game']['restart_poll']['yes'], [0])
        # A concurrently submitted second vote can use the original revision.
        response = await self.peers[1].act('restart_vote', ballot, revision=original_revision)
        self.assertEqual(response['type'], 'ack')
        s = self.room.game.data
        self.assertEqual(s['round'], 0)
        self.assertEqual(s['phase'], 'lobby')
        self.assertIsNone(s['restart_poll'])
        self.assertEqual(s['tricks'], [])
        self.assertEqual(s['discard'], [])
        self.assertEqual([len(h) for h in s['hands']], [0] * 4)
        self.assertEqual(s['kitty'], [])
        self.assertEqual([p['total2'] for p in self.room.players], [0] * 4)
        response = await self.peers[2].act('restart_vote', {'round': round_number})
        self.assertEqual(response['type'], 'error')
        self.assertEqual(s.get('restart_votes', []), [])

    async def test_vote_rejection_timeout_cooldown_and_old_ballot(self):
        await self.start()
        await self.act(0, 'restart_vote', {'round': 1})
        ballot = {'poll_id': self.room.game.data['restart_poll']['id'], 'choice': 'no'}
        for seat in [1, 2, 3]:
            await self.act(seat, 'restart_vote', ballot)
        self.assertIsNone(self.room.game.data['restart_poll'])
        self.assertAlmostEqual(self.room.game.data['restart_cooldown'] - time.time(), 120, delta=1)
        self.assertEqual((await self.peers[0].act('restart_vote', {'round': 1}))['type'], 'error')
        async with self.room.lock:
            self.room.game.data['restart_cooldown'] = time.time() - 1
        await self.act(0, 'restart_vote', {'round': 1})
        self.assertEqual((await self.peers[1].act('restart_vote', {**ballot, 'choice': 'yes'}))['type'], 'error')
        async with self.room.lock:
            self.room.game.data['restart_poll']['deadline'] = time.time() - 1
        await self.peers[1].wait(lambda s: s['restart_poll'] is None)
        self.assertGreater(self.room.game.data['restart_cooldown'], time.time() + 118)
        self.assertEqual(self.room.game.data['phase'], 'bidding')

    async def test_hidden_contract_privacy_and_public_history(self):
        for contract in ['sans', 'contre']:
            async with self.room.lock:
                self.room.transaction(self.room.reset_to_lobby)
                await self.room.broadcast()
            await self.start()
            taker = self.room.game.data['turn']
            hand = list(self.room.game.data['hands'][taker])
            await self.act(taker, 'bid', {'bid': contract})
            while self.room.game.data['phase'] == 'bidding':
                await self.act(self.room.game.data['turn'], 'bid', {'bid': 'pass'})
            self.assertEqual(self.room.game.data['hands'][taker], hand)
            for peer in self.peers:
                self.assertEqual(peer.state['kitty'], [])
                self.assertEqual(peer.state['discard'], [])
            for _ in range(4):
                seat = self.room.game.data['turn']
                await self.act(seat, 'play', {'card': self.peers[seat].state['legal'][0]})
            await self.peers[0].wait(lambda s: s['completed'] == 1)
            for peer in self.peers:
                await peer.wait(lambda s: s['completed'] == 1)
                self.assertEqual(len(peer.state['tricks'][0]['plays']), 4)
                self.assertEqual(peer.state['kitty'], [])
        self.assertEqual(self.room.durations['playing'], 60)

    async def test_chat_gifts_and_music_routes(self):
        deadline = self.room.deadline
        await self.act(0, 'chat', {'text': '这啥牌'})
        self.assertEqual(self.peers[3].state['social_events'][-1]['text'], '这啥牌')
        self.assertEqual(self.room.deadline, deadline)
        self.assertEqual((await self.peers[0].act('chat', {'text': 'spam'}))['type'], 'error')
        self.assertEqual((await self.peers[1].act('chat', {'text': '<script>'}))['type'], 'error')
        await self.act(1, 'gift', {'target': 0, 'gift': 'flower'})
        self.assertEqual(self.peers[2].state['social_events'][-1]['target_name'], '玩家1')
        self.assertEqual((await self.peers[2].act('gift', {'target': 2, 'gift': 'egg'}))['type'], 'error')
        await self.act(2, 'gift', {'target': 3, 'gift': 'egg'})
        for filename in ['leschampselysee', 'seikobeatclub', 'jiesuanyinyue']:
            response = await self.client.get(f'/musics/{filename}.mp3', headers={'Range': 'bytes=0-1023'})
            self.assertEqual(response.status, 206)
            self.assertEqual(len(await response.read()), 1024)
        self.assertEqual((await self.client.get('/musics/unknown.mp3')).status, 404)

    async def test_private_discard_and_award_names(self):
        await self.start()
        async with self.room.lock:
            for stats in self.room.game.data['stats']:
                stats['passes'] = 4
        for _ in range(4):
            await self.act(self.room.game.data['turn'], 'bid', {'bid': 'pass'})
        event = self.peers[0].state['announcements'][-1]
        self.assertEqual(event['kind'], 'turtle')
        self.assertEqual(event['names'], ['玩家1', '玩家2', '玩家3', '玩家4'])
        await self.take()
        taker = self.room.game.data['taker']
        for i, peer in enumerate(self.peers):
            self.assertEqual(len(peer.state['kitty']), 6)
            self.assertEqual(len(peer.state['discard']), 6 if i == taker else 0)

    async def test_finished_leave_preserves_result_names_and_allows_replacement(self):
        await self.start()
        await self.take()
        async with self.room.lock:
            while self.room.game.data['phase'] != 'finished':
                self.room.transaction(self.room.game.timeout)
            await self.room.broadcast()
        old_result = self.room.game.data['result']
        await self.client.post('/api/leave', json={'token': self.tokens[0]})
        self.assertEqual(self.room.host, 1)
        replacement = await (await self.client.post('/api/join', json={'name': '第五位好友'})).json()
        self.tokens[0] = replacement['token']
        self.peers[0] = await self.connect(replacement['token'])
        self.assertEqual(self.peers[0].state['result'], old_result)
        self.assertEqual(old_result['players'][0]['name'], '玩家1')
        self.assertEqual(self.peers[0].state['players'][0]['total'], 0)
        self.assertFalse(self.peers[0].state['players'][0]['played_this_round'])
        self.assertEqual(self.peers[0].state['players'][0]['tricks'], 0)
        await self.sync()
        self.assertEqual((await self.peers[0].act('restart_vote', {'round': 1}))['type'], 'error')
        await self.act(self.room.host, 'start')
        self.assertEqual(self.room.game.data['phase'], 'bidding')

    async def test_all_leave_outside_resets_room(self):
        for token in self.tokens:
            await self.client.post('/api/leave', json={'token': token})
        self.assertEqual((await (await self.client.get('/api/lobby')).json())['occupied'], 0)
        self.assertEqual(self.room.players, [None] * 4)
        self.assertIsNone(self.room.host)
        self.assertTrue(self.room.paused)
        self.assertEqual(self.room.storage.load()['players'], [None] * 4)


if __name__ == '__main__':
    unittest.main()
