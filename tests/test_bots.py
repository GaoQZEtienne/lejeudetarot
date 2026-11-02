import asyncio
from copy import deepcopy
import random
import threading
import time
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from game.bots import BotDecision, RandomPolicy
from game.rules import auto_discard, RuleError
from game.storage import Storage
from server import ROOM_KEY, Room, create_app
from tests.test_room import Peer


class BotObservationTests(unittest.TestCase):
    def setUp(self):
        self.room = Room(Storage(':memory:'))
        for i in range(4):
            self.room.join(f'玩家{i}')

    def tearDown(self):
        self.room.storage.close()

    def test_private_observation_and_copy_isolation(self):
        room = self.room
        for contract in ('prise', 'garde', 'jiedi', 'sans', 'contre'):
            room.game.new_deal()
            taker = room.game.data['turn']
            room.game.action(taker, 'bid', {'bid': contract})
            while room.game.data['phase'] == 'bidding':
                room.game.action(room.game.data['turn'], 'bid', {'bid': 'pass'})
            for seat in range(4):
                observation = room.bot_observation(seat)
                self.assertEqual(observation['hand'], room.game.data['hands'][seat])
                self.assertEqual(observation['kitty'], room.game.data['kitty'] if contract in ('prise', 'garde', 'jiedi') else [])
                self.assertNotIn('hands', observation)
                self.assertNotIn('draft', observation)
                self.assertTrue(all('token_hash' not in p and 'hand' not in p for p in observation['players']))
                observation['hand'].clear()
                observation['players'][0]['name'] = 'modified'
                self.assertEqual(len(room.game.data['hands'][seat]), 18)
                self.assertEqual(room.players[0]['name'], '玩家0')
            if room.game.data['phase'] == 'discard':
                cards = auto_discard(room.game.data['hands'][taker])
                room.game.action(taker, 'discard', {'cards': cards})
                for seat in range(4):
                    self.assertEqual(room.bot_observation(seat)['discard'], cards if seat == taker else [])

    def test_random_policy_completes_all_contracts_through_same_interface(self):
        policy = RandomPolicy(random.Random(17))
        for contract in ('prise', 'garde', 'jiedi', 'sans', 'contre'):
            self.room.game.new_deal()
            self.room.game.action(self.room.game.data['turn'], 'bid', {'bid': contract})
            while self.room.game.data['phase'] != 'finished':
                state = self.room.game.data
                if state['turn'] is not None:
                    decision = policy.choose(self.room.bot_observation(state['turn']))
                    self.room.game.action(state['turn'], decision.action, decision.payload)
                else:
                    self.room.game.timeout()
            result = self.room.game.data['result']
            self.assertEqual(len(self.room.game.data['tricks']), 18)
            self.assertEqual(result['points'] + result['defense_points'], 91)
            self.assertEqual(sum(result['scores']), 0)


class BotRoomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(':memory:', {'bot': .01, 'collecting': .01, 'revealing': .01})
        self.room = self.app[ROOM_KEY]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.peers = []
        self.tokens = []
        await self.add_human('房主')

    async def add_human(self, name):
        response = await self.client.post('/api/join', json={'name': name})
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.tokens.append(data['token'])
        peer = await self.connect(data['token'])
        self.peers.append(peer)
        return peer

    async def connect(self, token):
        ws = await self.client.ws_connect('/ws')
        await ws.send_json({'token': token})
        peer = Peer(ws)
        await peer.wait(lambda s: True)
        return peer

    async def asyncTearDown(self):
        await asyncio.gather(*(p.close() for p in self.peers))
        await self.client.close()

    async def act(self, action, payload=None, peer=None):
        peer = peer or self.peers[0]
        if action in ('bot_add', 'bot_fill'):
            payload = {'policy': 'random', **(payload or {})}
        await peer.wait(lambda s: s['revision'] == self.room.revision)
        response = await peer.act(action, payload)
        self.assertEqual(response['type'], 'ack', response)
        await peer.wait(lambda s: s['revision'] == self.room.revision)
        return peer.state

    async def test_manage_permissions_capacity_policy_and_tokens(self):
        guest = await self.add_human('好友')
        await guest.wait(lambda s: s['revision'] == self.room.revision)
        self.assertEqual((await guest.act('bot_fill'))['type'], 'error')
        await self.act('bot_add', {'seat': 3})
        self.assertTrue(self.room.is_bot(3))
        self.assertFalse(self.room.connections[3])
        self.assertTrue(self.room.online(3))
        self.assertNotIn('token_hash', self.room.players[3])
        self.assertIsNone(self.room.identify('not-a-bot-token-but-long-enough'))
        for payload in ({'seat': True}, {'seat': -1}, {'seat': 0}, {'seat': []}, {'seat': 2, 'policy': 'missing'}, {'seat': 2, 'policy': []}):
            response = await self.peers[0].act('bot_add', payload)
            self.assertEqual(response['type'], 'error', payload)
        await self.act('bot_fill')
        lobby = await (await self.client.get('/api/lobby')).json()
        self.assertEqual((lobby['occupied'], lobby['humans'], lobby['bots']), (4, 2, 2))
        self.assertEqual(len({p['name'] for p in self.room.players}), 4)
        self.assertEqual((await self.peers[0].act('bot_fill'))['type'], 'error')
        for seat in (0, True, 5):
            self.assertEqual((await self.peers[0].act('bot_remove', {'seat': seat}))['type'], 'error')
        await self.act('start')
        self.assertEqual((await self.peers[0].act('bot_remove', {'seat': 3}))['type'], 'error')
        with self.assertRaises(RuleError):
            self.room.handle_action(3, {'action': 'restart_vote', 'payload': {'round': 1}})

    async def test_remove_replace_and_last_human_leave_clears_bots(self):
        # Human names cannot collide with generated bot names.
        guest = await self.add_human('人机1')
        await self.act('bot_fill')
        self.assertEqual([p['name'] for p in self.room.players], ['房主', '人机1', '人机2', '人机3'])
        self.room.players[2]['total2'] = 80
        self.room.game.data['stats'][2]['wins'] = 2
        await self.act('bot_remove', {'seat': 2})
        replacement = await self.add_human('新好友')
        self.assertEqual(replacement.state['seat'], 2)
        self.assertEqual(self.room.players[2]['total2'], 0)
        self.assertEqual(self.room.game.data['stats'][2]['wins'], 0)
        await self.client.post('/api/leave', json={'token': self.tokens[0]})
        self.assertIn(self.room.host, (1, 2))
        self.assertFalse(self.room.is_bot(self.room.host))
        for token in self.tokens[1:]:
            await self.client.post('/api/leave', json={'token': token})
        self.assertEqual(self.room.players, [None] * 4)
        self.assertIsNone(self.room.host)
        self.assertTrue(self.room.paused)
        self.assertEqual(self.room.storage.load()['players'], [None] * 4)

    async def test_single_human_restart_and_pause_with_bots(self):
        self.room.bot_delay = 2
        await self.act('bot_fill')
        await self.act('start')
        before = deepcopy(self.room.game.data)
        await self.peers[0].close()
        self.assertTrue(self.room.paused)
        self.assertTrue(all(self.room.online(i) for i in (1, 2, 3)))
        self.assertIsNone(self.room.deadline)
        await asyncio.sleep(.35)
        self.assertEqual(self.room.game.data, before)
        self.peers[0] = await self.connect(self.tokens[0])
        self.assertFalse(self.room.paused)
        self.assertEqual(self.peers[0].state['restart_required'], 1)
        await self.act('restart_vote', {'round': self.room.game.data['round']})
        self.assertEqual(self.room.game.data['phase'], 'lobby')
        self.assertEqual(sum(self.room.is_bot(i) for i in range(4)), 3)
        self.assertEqual([p['total2'] for p in self.room.players], [0] * 4)

    async def test_two_humans_vote_without_bot_votes(self):
        self.room.bot_delay = 5
        guest = await self.add_human('好友')
        await self.act('bot_fill')
        await self.act('start')
        deadline = self.room.deadline
        await self.act('restart_vote', {'round': 1})
        self.assertEqual(self.room.deadline, deadline)
        poll = self.room.game.data['restart_poll']
        self.assertEqual((self.peers[0].state['restart_required'], self.peers[0].state['restart_reject_required']), (2, 1))
        await self.act('restart_vote', {'poll_id': poll['id'], 'choice': 'no'}, guest)
        self.assertIsNone(self.room.game.data['restart_poll'])
        self.assertGreater(self.room.game.data['restart_cooldown'], time.time())
        self.room.game.data['restart_cooldown'] = 0
        await self.act('restart_vote', {'round': 1})
        poll = self.room.game.data['restart_poll']
        await self.act('restart_vote', {'poll_id': poll['id']}, guest)
        self.assertEqual(self.room.game.data['phase'], 'lobby')

    async def test_host_transfer_never_selects_bot(self):
        guest = await self.add_human('好友')
        await self.act('bot_fill')
        await self.peers[0].close()
        self.room.offline_since[0] = time.time() - 31
        await guest.wait(lambda s: s['host'] == 1)
        self.assertFalse(self.room.is_bot(self.room.host))

    async def test_difficulty_defaults_permissions_and_mixed_policy_round(self):
        # Omitted policy defaults to normal for actual clients.
        response = await self.peers[0].act('bot_add')
        self.assertEqual(response['type'], 'ack')
        self.assertEqual(self.room.players[1]['policy'], 'normal')
        await self.act('bot_fill')
        self.room.players[1]['total2'] = 120
        self.room.game.data['stats'][1]['wins'] = 2
        await self.act('bot_set_policy', {'seat': 1, 'policy': 'advanced'})
        self.assertEqual(self.room.players[1]['total2'], 120)
        self.assertEqual(self.room.game.data['stats'][1]['wins'], 2)
        await self.act('bot_set_policy', {'seat': 2, 'policy': 'normal'})
        self.assertEqual(self.room.storage.load()['players'][1]['policy'], 'advanced')
        for payload in ({'seat': 0, 'policy': 'normal'}, {'seat': True, 'policy': 'normal'},
                        {'seat': 1, 'policy': 'missing'}):
            self.assertEqual((await self.peers[0].act('bot_set_policy', payload))['type'], 'error')
        await self.act('start')
        self.assertEqual((await self.peers[0].act('bot_set_policy', {'seat': 1, 'policy': 'normal'}))['type'], 'error')
        async with asyncio.timeout(45):
            while self.room.game.data['phase'] != 'finished':
                state = self.room.game.data
                if state['turn'] == 0:
                    view = await self.peers[0].wait(lambda s: s['revision'] == self.room.revision)
                    if state['phase'] == 'bidding':
                        await self.act('bid', {'bid': 'pass' if state['contract'] else 'prise'})
                    elif state['phase'] == 'discard':
                        await self.act('discard', {'cards': auto_discard(view['hand'])})
                    else:
                        await self.act('play', {'card': view['legal'][0]})
                else:
                    revision = self.room.revision
                    await self.peers[0].wait(lambda s: s['revision'] > revision)
        self.assertEqual(len(self.room.game.data['tricks']), 18)
        self.assertEqual(sum(self.room.game.data['result']['scores']), 0)
        await self.act('bot_remove', {'seat': 3})
        guest = await self.add_human('好友')
        self.assertEqual((await guest.act('bot_set_policy', {'seat': 1, 'policy': 'normal'}))['type'], 'error')

    async def test_worker_does_not_hold_lock_and_stale_result_is_discarded(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        class SlowPolicy:
            def choose(self, observation):
                entered.set()
                release.wait(3)
                finished.set()
                return BotDecision('bid', {'bid': 'prise'})

        await self.act('bot_fill')
        async with self.room.lock:
            self.room.transaction(self.room.game.new_deal)
            self.room.game.data['turn'] = 1
            await self.room.broadcast()
        try:
            with patch.dict('server.POLICIES', {'random': SlowPolicy()}):
                async with asyncio.timeout(2):
                    while not entered.is_set():
                        await asyncio.sleep(.01)
                # Voting remains responsive while the policy is blocked in a worker.
                await asyncio.wait_for(self.act('restart_vote', {'round': 1}), .5)
                self.assertEqual(self.room.game.data['phase'], 'lobby')
                release.set()
                async with asyncio.timeout(2):
                    while not finished.is_set():
                        await asyncio.sleep(.01)
                await asyncio.sleep(.25)
                self.assertEqual(self.room.game.data['phase'], 'lobby')
                self.assertIsNone(self.room.game.data['taker'])
        finally:
            release.set()

    async def test_invalid_policy_action_falls_back_to_legal_play(self):
        class InvalidPolicy:
            def choose(self, observation):
                return BotDecision('play', {'card': 'not-a-card'})

        await self.act('bot_fill')
        async with self.room.lock:
            self.room.transaction(self.room.game.new_deal)
            self.room.game.action(self.room.game.data['turn'], 'bid', {'bid': 'contre'})
            self.room.game.data['turn'] = 1
            await self.room.broadcast()
        legal = self.room.view(1)['legal']
        with patch.dict('server.POLICIES', {'random': InvalidPolicy()}), self.assertLogs('tarot', level='ERROR'):
            await self.peers[0].wait(lambda s: bool(s['trick']))
        self.assertIn(self.room.game.data['trick'][0]['card'], legal)
        self.assertEqual(self.room.game.data['trick'][0]['seat'], 1)

    async def test_complete_round_with_each_number_of_bots(self):
        # Run the real scheduler/WS interface with 3, then 2, then 1 bot.
        for bot_count in (3, 2, 1):
            with self.subTest(bots=bot_count):
                if any(p is None for p in self.room.players):
                    await self.act('bot_fill')
                totals_before = [p['total2'] / 2 for p in self.room.players]
                await self.act('start')
                async with asyncio.timeout(45):
                    while self.room.game.data['phase'] != 'finished':
                        state = self.room.game.data
                        seat = state['turn']
                        if seat is not None and not self.room.is_bot(seat):
                            peer = next(p for p in self.peers if p.state['seat'] == seat)
                            await peer.wait(lambda s: s['revision'] == self.room.revision)
                            if state['phase'] == 'bidding':
                                payload = {'bid': 'pass' if state['contract'] else 'prise'}
                                await self.act('bid', payload, peer)
                            elif state['phase'] == 'discard':
                                await self.act('discard', {'cards': auto_discard(peer.state['hand'])}, peer)
                            else:
                                await self.act('play', {'card': peer.state['legal'][0]}, peer)
                        else:
                            revision = self.room.revision
                            await self.peers[0].wait(lambda s: s['revision'] > revision)
                result = self.room.game.data['result']
                self.assertEqual(len(self.room.game.data['tricks']), 18)
                self.assertEqual(result['points'] + result['defense_points'], 91)
                self.assertEqual(sum(result['scores']), 0)
                self.assertEqual([p['total2'] / 2 for p in self.room.players],
                                 [before + score for before, score in zip(totals_before, result['scores'])])
                self.assertEqual(self.room.storage.load()['game']['result'], result)
                if bot_count > 1:
                    previous_result = deepcopy(result)
                    target = next(i for i in range(4) if self.room.is_bot(i))
                    await self.act('bot_remove', {'seat': target})
                    await self.add_human(f'好友{len(self.peers)}')
                    self.assertEqual(self.room.game.data['result'], previous_result)
                    self.assertFalse(self.room.view(target)['players'][target]['played_this_round'])


if __name__ == '__main__':
    unittest.main()
