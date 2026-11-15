import asyncio
from copy import deepcopy
import time
import unittest

from tests import test_room as support


class RoomUpdatesTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.RoomTests.asyncSetUp
    asyncTearDown = support.RoomTests.asyncTearDown
    connect = support.RoomTests.connect
    sync = support.RoomTests.sync
    act = support.RoomTests.act
    start = support.RoomTests.start

    async def test_all_pass_reveal_reconnect_deadline_and_new_deal_privacy(self):
        await self.start()
        original = deepcopy(self.room.game.data['hands'])
        kitty = list(self.room.game.data['kitty'])
        self.assertTrue(all(p.state['revealed_hands'] == [] for p in self.peers))
        for _ in range(4):
            await self.act(self.room.game.data['turn'], 'bid', {'bid': 'pass'})
        deadline = self.room.deadline
        self.assertAlmostEqual(deadline - time.time(), 10, delta=1)
        for peer in self.peers:
            self.assertEqual(peer.state['revealed_hands'], original)
            self.assertEqual(peer.state['kitty'], kitty)
            self.assertIsNone(peer.state['result'])
        twin = await self.connect(self.tokens[1])
        self.assertEqual(twin.state['revealed_hands'], original)
        self.assertEqual(self.room.deadline, deadline)
        await twin.close()
        async with self.room.lock:
            self.room.deadline = time.time() - 1
        await self.peers[0].wait(lambda s: s['round'] == 2)
        await self.sync()
        for peer in self.peers:
            self.assertEqual(peer.state['revealed_hands'], [])
            self.assertEqual(peer.state['kitty'], [])
            self.assertEqual(peer.state['phase'], 'bidding')

    async def test_kick_outside_revokes_all_tabs_and_releases_for_replacement(self):
        twin = await self.connect(self.tokens[1])
        self.assertEqual((await self.peers[2].act('kick', {'seat': 1}))['type'], 'error')
        for target in (0, True, -1, 8):
            self.assertEqual((await self.peers[0].act('kick', {'seat': target}))['type'], 'error')
        response = await self.peers[0].act('kick', {'seat': 1})
        self.assertEqual(response['type'], 'ack')
        self.assertIsNone(self.room.identify(self.tokens[1]))
        self.assertIsNone(self.room.players[1])
        self.assertFalse(self.room.connections[1])
        self.assertEqual((await asyncio.wait_for(twin.responses.get(), 2))['type'], 'released')
        await twin.close()
        response = await self.client.post('/api/join', json={'name': '新终端玩家'})
        joined = await response.json()
        self.assertEqual(joined['seat'], 1)
        self.tokens[1] = joined['token']
        self.peers[1] = await self.connect(joined['token'])
        self.assertEqual(self.room.players[1]['total2'], 0)

    async def test_ingame_kick_keeps_cards_and_replaces_with_normal_bot(self):
        self.room.bot_delay = 5
        await self.start()
        hands = deepcopy(self.room.game.data['hands'])
        deadline = self.room.deadline
        self.room.players[1]['total2'] = 50
        response = await self.peers[0].act('kick', {'seat': 1})
        self.assertEqual(response['type'], 'ack')
        self.assertTrue(self.room.is_bot(1))
        self.assertEqual(self.room.players[1]['policy'], 'normal')
        self.assertEqual(self.room.players[1]['total2'], 50)
        self.assertEqual(self.room.game.data['hands'], hands)
        self.assertEqual(self.room.deadline, deadline)
        self.assertIsNone(self.room.identify(self.tokens[1]))
        self.assertFalse(self.room.connections[1])
        self.assertEqual((await self.client.post('/api/join', json={'name': '局内不能补位'})).status, 400)

    async def test_emote_whitelist_rate_limit_broadcast_and_ephemeral_storage(self):
        await self.act(0, 'emote', {'emoji': '😂'})
        for peer in self.peers:
            self.assertEqual(peer.state['social_events'][-1]['kind'], 'emote')
            self.assertEqual(peer.state['social_events'][-1]['text'], '😂')
        self.assertEqual((await self.peers[0].act('emote', {'emoji': '❤️'}))['type'], 'error')
        self.assertEqual((await self.peers[1].act('emote', {'emoji': '<script>'}))['type'], 'error')
        self.assertNotIn('social_events', self.room.storage.load())
        self.room.social_events[0]['time'] -= 6
        self.assertEqual(self.room.view(0)['social_events'], [])
