"""Optional real-Edge acceptance run; uses a separate temporary room.

Run: .venv/Scripts/python.exe tests/browser_smoke.py
Requires playwright and an installed Microsoft Edge; no browser download needed.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright

from server import ROOM_KEY, create_app


async def wait_state(page, expression):
    # Keep the site's CSP intact: Playwright's wait_for_function uses in-page eval.
    async with asyncio.timeout(15):
        while not await page.evaluate('() => (' + expression + ')'):
            await asyncio.sleep(0.03)


async def main():
    artifacts = Path(__file__).resolve().parents[1] / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    errors = []
    with tempfile.TemporaryDirectory() as temp:
        app = create_app(str(Path(temp) / 'browser.sqlite3'), {'collecting': 0.05, 'playing': 300, 'discard': 300, 'bidding': 300})
        server = TestServer(app)
        await server.start_server()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel='msedge', headless=True)
                try:
                    contexts = [await browser.new_context(viewport={'width': 1440, 'height': 1050}) for _ in range(4)]
                    pages = [await context.new_page() for context in contexts]
                    for page in pages:
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                    url = str(server.make_url('/'))
                    await pages[0].goto(url)
                    await pages[0].screenshot(path=str(artifacts / 'welcome-desktop.png'), full_page=True)
                    for i, page in enumerate(pages):
                        await page.goto(url)
                        await page.locator('#nickname').fill(['春山', '林间', '小满', '青禾'][i])
                        await page.locator('#join-button').click()
                        await page.locator('#game').wait_for(state='visible')
                    room = app[ROOM_KEY]
                    # Outside a deal, leave revokes the identity and frees a seat.
                    await pages[0].get_by_role('button', name='离开房间', exact=True).click()
                    await pages[0].locator('#welcome').wait_for(state='visible')
                    await wait_state(pages[1], 'state.players[0] === null && state.host === 1')
                    await pages[0].locator('#join-form').wait_for(state='visible')
                    assert await pages[0].locator('#resume-button').is_hidden()
                    assert await pages[0].locator('#nickname').input_value() == ''
                    assert await pages[0].evaluate("localStorage.getItem('tarot-seat-token')") is None
                    await pages[0].reload()
                    await pages[0].locator('#join-form').wait_for(state='visible')
                    await pages[0].evaluate("window.dispatchEvent(new Event('online'))")
                    await asyncio.sleep(1.2)
                    assert await pages[0].locator('#welcome').is_visible()
                    assert not await pages[0].locator('#game').is_visible()
                    assert await pages[0].locator('#occupancy').inner_text() == '3 / 4 已入座'
                    await pages[0].screenshot(path=str(artifacts / 'leave-welcome.png'), full_page=True)
                    # A fifth independent browser can now occupy the vacated seat.
                    replacement_context = await browser.new_context(viewport={'width': 1440, 'height': 1050})
                    contexts.append(replacement_context)
                    pages[0] = await replacement_context.new_page()
                    pages[0].on('pageerror', lambda error: errors.append(str(error)))
                    pages[0].on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                    await pages[0].goto(url)
                    await pages[0].locator('#nickname').fill('秋水')
                    await pages[0].locator('#join-button').click()
                    await pages[0].locator('#game').wait_for(state='visible')
                    assert await pages[0].evaluate('state.seat') == 0
                    await wait_state(pages[1], 'currentTrack === "leschampselysee" && !music.paused')
                    await pages[0].locator('#chat-open').click()
                    await pages[0].get_by_role('button', name='这啥牌', exact=True).click()
                    for page in pages:
                        await page.locator('.speech-bubble[data-speaker="0"]').wait_for(state='visible')
                        assert await page.locator('.speech-bubble[data-speaker="0"]').inner_text() == '这啥牌'
                    await pages[1].locator('[data-seat="0"] button.avatar').click()
                    await pages[1].locator('#flower-button').click()
                    await pages[0].locator('.gift-flight').wait_for(state='visible')
                    await pages[2].locator('[data-seat="0"] button.avatar').click()
                    await pages[2].locator('#egg-button').click()
                    await wait_state(pages[0], 'state.social_events.some(e => e.gift === "egg")')
                    await pages[0].screenshot(path=str(artifacts / 'table-social.png'), full_page=True)
                    await pages[0].locator('.speech-bubble').wait_for(state='detached', timeout=6500)
                    assert await pages[0].locator('#social-log').count() == 0
                    assert await pages[0].locator('#history-open').count() == 0
                    await pages[room.host].get_by_role('button', name='开始游戏 →').click()
                    await wait_state(pages[0], "state.phase === 'bidding'")
                    deadline_before_vote = room.deadline
                    await pages[0].get_by_role('button', name='重开投票', exact=True).click()
                    await wait_state(pages[1], 'state.restart_poll?.yes.length === 1')
                    assert room.deadline == deadline_before_vote
                    assert await pages[0].get_by_role('button', name='已同意', exact=True).is_disabled()
                    await pages[0].reload()
                    await pages[0].locator('#game').wait_for(state='visible')
                    assert await pages[0].get_by_role('button', name='已同意', exact=True).is_disabled()
                    await pages[1].screenshot(path=str(artifacts / 'restart-vote.png'), full_page=True)
                    await pages[1].get_by_role('button', name='同意重开', exact=True).click()
                    await wait_state(pages[0], "state.round === 0 && state.phase === 'lobby'")
                    assert room.game.data['restart_poll'] is None
                    assert all(p['total2'] == 0 for p in room.players)
                    await pages[room.host].get_by_role('button', name='开始游戏 →').click()
                    await wait_state(pages[0], "state.phase === 'bidding'")
                    bidder = room.game.data['turn']
                    await wait_state(pages[bidder], 'state.turn === state.seat && !pending')
                    await pages[bidder].get_by_role('button', name='Prise ×1', exact=True).click()
                    await wait_state(pages[bidder], '!pending')
                    while room.game.data['phase'] == 'bidding':
                        current = pages[room.game.data['turn']]
                        await wait_state(current, 'state.turn === state.seat && !pending')
                        await current.get_by_role('button', name='不叫', exact=True).click()
                        await wait_state(current, '!pending')
                    await wait_state(pages[bidder], "state.phase === 'discard' && !pending")
                    await pages[bidder].screenshot(path=str(artifacts / 'discard-desktop.png'), full_page=True)
                    original = await pages[bidder].evaluate('state.hand.slice()')
                    kitty = await pages[bidder].evaluate('state.kitty.slice()')
                    assert not set(original) & set(kitty)
                    for _ in range(6):
                        await pages[bidder].locator('#hand button:not(:disabled):not(.selected)').first.click()
                        await wait_state(pages[bidder], '!pending')
                    await pages[bidder].get_by_role('button', name='确认扣六张').click()
                    await wait_state(pages[bidder], "state.phase === 'playing'")
                    await wait_state(pages[bidder], 'currentTrack === "seikobeatclub"')
                    assert set(kitty) <= set(await pages[bidder].evaluate('state.hand'))
                    await pages[bidder].locator('#discard-open').click()
                    assert await pages[bidder].locator('#inspect-content .playing-card').count() == 6
                    await pages[bidder].get_by_role('button', name='关闭查牌', exact=True).click()
                    await pages[(bidder + 1) % 4].locator('#kitty-open').click()
                    assert await pages[(bidder + 1) % 4].locator('#inspect-content .playing-card').count() == 6
                    await pages[(bidder + 1) % 4].get_by_role('button', name='关闭查牌', exact=True).click()
                    assert await pages[(bidder + 1) % 4].locator('#discard-open').is_hidden()
                    await pages[bidder].reload()
                    await pages[bidder].locator('#game').wait_for(state='visible')
                    assert await pages[bidder].evaluate('state.seat') == bidder
                    hand_before = await pages[bidder].evaluate('state.hand.slice()')
                    deadline_before = room.deadline
                    await pages[bidder].get_by_role('button', name='离开房间', exact=True).click()
                    await pages[bidder].locator('#welcome').wait_for(state='visible')
                    assert await pages[bidder].evaluate('music.paused && currentTrack === ""')
                    await wait_state(pages[(bidder + 1) % 4], f'!state.players[{bidder}].online')
                    await pages[bidder].reload()
                    await pages[bidder].locator('#resume-button').wait_for(state='visible')
                    assert await pages[bidder].locator('#join-form').is_hidden()
                    await pages[bidder].evaluate("window.dispatchEvent(new Event('online'))")
                    await asyncio.sleep(1.2)
                    assert await pages[bidder].locator('#welcome').is_visible()
                    await pages[bidder].get_by_role('button', name='返回我的座位 →').click()
                    await pages[bidder].locator('#game').wait_for(state='visible')
                    assert await pages[bidder].evaluate('state.hand') == hand_before
                    assert room.deadline == deadline_before
                    await pages[0].screenshot(path=str(artifacts / 'table-desktop.png'), full_page=True)
                    await pages[0].set_viewport_size({'width': 390, 'height': 844})
                    assert await pages[0].evaluate('document.documentElement.scrollWidth <= innerWidth')
                    await pages[0].screenshot(path=str(artifacts / 'table-mobile.png'), full_page=True)
                    await pages[0].get_by_role('button', name='玩法说明').click()
                    await pages[0].locator('#rules-dialog').wait_for(state='visible')
                    await pages[0].get_by_role('button', name='关闭玩法说明').click()
                    await pages[0].set_viewport_size({'width': 1440, 'height': 1050})
                    played = 0
                    while room.game.data['phase'] != 'finished':
                        if room.game.data['phase'] == 'collecting':
                            await wait_state(pages[0], "state.phase !== 'collecting'")
                            continue
                        seat = room.game.data['turn']
                        page = pages[seat]
                        await wait_state(page, "state.turn === state.seat && state.phase === 'playing' && !pending")
                        await page.locator('#hand button:not(:disabled)').first.click()
                        await page.get_by_role('button', name='出牌 →', exact=True).click()
                        await wait_state(page, '!pending')
                        played += 1
                        if played == 3:
                            await pages[0].screenshot(path=str(artifacts / 'trick-desktop.png'), full_page=True)
                            await pages[0].set_viewport_size({'width': 390, 'height': 844})
                            await pages[0].screenshot(path=str(artifacts / 'trick-mobile.png'), full_page=True)
                            await pages[0].set_viewport_size({'width': 1440, 'height': 1050})
                    await pages[0].locator('#result').wait_for(state='visible')
                    await wait_state(pages[0], 'currentTrack === "jiesuanyinyue"')
                    await pages[0].locator('[data-seat="0"] .trick-link').click()
                    for seat_index in range(4):
                        await pages[0].locator('#inspect-tabs button').nth(seat_index).click()
                        expected = sum(t['winner'] == seat_index for t in room.game.data['tricks'])
                        actual = await pages[0].locator('#inspect-content h3').all_text_contents()
                        assert sum(text.startswith('第 ') for text in actual) == expected
                    await pages[0].get_by_role('button', name='关闭查牌', exact=True).click()
                    assert played == 72, played
                    assert room.game.data['result']['points'] + room.game.data['result']['defense_points'] == 91
                    assert sum(room.game.data['result']['scores']) == 0
                    await pages[0].screenshot(path=str(artifacts / 'result-desktop.png'), full_page=True)
                    await pages[0].get_by_role('button', name='查看上一墩').click()
                    await pages[0].locator('#last-dialog').wait_for(state='visible')
                    await pages[0].get_by_role('button', name='关闭上一墩').click()
                    await pages[0].get_by_role('button', name='离开房间', exact=True).click()
                    await pages[0].locator('#welcome').wait_for(state='visible')
                    await pages[0].locator('#join-form').wait_for(state='visible')
                    assert await pages[0].locator('#resume-button').is_hidden()
                    await pages[0].locator('#nickname').fill('归客')
                    await pages[0].locator('#join-button').click()
                    await pages[0].locator('#result').wait_for(state='visible')
                    assert await pages[0].evaluate('state.players[0].total') == 0
                    assert await pages[0].evaluate('state.result.players[0].name') == '秋水'
                    await pages[room.host].get_by_role('button', name='开始下一局 →').click()
                    await wait_state(pages[0], "state.phase === 'bidding' && state.round === 2")
                    # Reject a poll, check cooldown and the final-five-second tone.
                    await pages[0].locator('#restart-button').click()
                    for i in [1, 2, 3]:
                        await wait_state(pages[i], 'Boolean(state.restart_poll) && !pending')
                        await pages[i].locator('#restart-no').click()
                    await wait_state(pages[0], '!state.restart_poll && state.restart_cooldown > state.server_time')
                    assert await pages[0].locator('#restart-button').is_disabled()
                    async with room.lock:
                        room.deadline = __import__('time').time() + 4.8
                        await room.broadcast()
                    await wait_state(pages[1], 'lastBeep.endsWith(":5") || lastBeep.endsWith(":4")')
                    assert await pages[1].evaluate('audioContext.state') == 'running'
                    async with room.lock:
                        room.set_deadline()
                        for stats in room.game.data['stats']:
                            stats['passes'] = 4
                        await room.broadcast()
                    for _ in range(4):
                        current = pages[room.game.data['turn']]
                        await wait_state(current, 'state.turn === state.seat && !pending')
                        await current.get_by_role('button', name='不叫', exact=True).click()
                        await wait_state(current, '!pending')
                    for page in pages:
                        await page.locator('#award-dialog').wait_for(state='visible')
                        assert await page.locator('#award-title').inner_text() == '玄武制胜'
                        assert len((await page.locator('#award-names').inner_text()).split('、')) == 4
                    await pages[0].screenshot(path=str(artifacts / 'achievement.png'), full_page=True)
                    for page in pages:
                        await page.locator('#award-close').click()
                    # Exercise the queued presentation of the other award types.
                    async with room.lock:
                        for kind in ['streak', 'taker', 'bidder']:
                            room.transaction(lambda kind=kind: room.game.announce(kind, [0, 1]), reset_clock=False)
                        await room.broadcast()
                    for title in ['TA的连胜，我很怕', '不要，妨碍我', '我叫，我再叫！']:
                        for page in pages:
                            await page.locator('#award-dialog').wait_for(state='visible')
                            assert await page.locator('#award-title').inner_text() == title
                            await page.locator('#award-close').click()
                    current = pages[room.game.data['turn']]
                    await current.get_by_role('button', name='Garde sans ×4', exact=True).click()
                    await wait_state(current, '!pending')
                    while room.game.data['phase'] == 'bidding':
                        current = pages[room.game.data['turn']]
                        await wait_state(current, 'state.turn === state.seat && !pending')
                        await current.get_by_role('button', name='不叫', exact=True).click()
                        await wait_state(current, '!pending')
                    for page in pages:
                        await wait_state(page, 'state.phase === "playing" && currentTrack === "seikobeatclub"')
                        assert await page.locator('#kitty-open').is_hidden()
                        assert await page.locator('#discard-open').is_hidden()
                    await pages[0].set_viewport_size({'width': 390, 'height': 844})
                    assert await pages[0].evaluate('document.documentElement.scrollWidth <= innerWidth')
                    await pages[0].screenshot(path=str(artifacts / 'modi-mobile.png'), full_page=True)
                    # End the hidden-kitty deal through the real engine. The UI
                    # must show six cards for ten seconds before score/winner UI.
                    async with room.lock:
                        while room.game.data['phase'] != 'revealing':
                            room.transaction(room.game.timeout)
                        reveal_deadline = room.deadline
                        old_totals = [p['total2'] for p in room.players]
                        await room.broadcast()
                    for page in pages:
                        await page.locator('#reveal-panel').wait_for(state='visible')
                        assert await page.locator('#reveal-cards .playing-card').count() == 6
                        assert await page.locator('#result').is_hidden()
                        assert await page.evaluate('state.result') is None
                    assert reveal_deadline - __import__('time').time() > 8
                    await pages[0].screenshot(path=str(artifacts / 'kitty-reveal-mobile.png'), full_page=True)
                    assert await pages[0].evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert await pages[0].evaluate('''() => {
                        const reveal = document.querySelector('#reveal-panel').getBoundingClientRect();
                        const left = document.querySelector('.seat.left').getBoundingClientRect();
                        const right = document.querySelector('.seat.right').getBoundingClientRect();
                        return left.right <= reveal.left && right.left >= reveal.right;
                    }'''), 'Mobile reveal panel must not cover player names or trick links'
                    await asyncio.sleep(2)
                    assert [p['total2'] for p in room.players] == old_totals
                    assert room.game.data['phase'] == 'revealing'
                    await pages[1].reload()
                    await pages[1].locator('#reveal-panel').wait_for(state='visible')
                    assert room.deadline == reveal_deadline
                    await wait_state(pages[0], 'state.phase === "finished"')
                    assert __import__('time').time() >= reveal_deadline
                    assert await pages[0].locator('#reveal-panel').is_hidden()
                    # Dismiss any legitimate end-of-deal achievement before cleanup.
                    for page in pages:
                        await wait_state(page, 'state.phase === "finished"')
                        while await page.locator('#award-dialog').is_visible():
                            await page.locator('#award-close').click()
                    await pages[room.host].get_by_role('button', name='开始下一局 →').click()
                    await wait_state(pages[0], 'state.phase === "bidding"')
                    # Finish browser WebSocket handshakes before closing Edge's pipes on Windows.
                    await asyncio.gather(*(p.get_by_role('button', name='离开房间', exact=True).click() for p in pages))
                    await asyncio.gather(*(p.locator('#welcome').wait_for(state='visible') for p in pages))
                    async with asyncio.timeout(5):
                        while not room.paused:
                            await asyncio.sleep(0.03)
                    # Restart on the same origin: cached identities and resume pages
                    # must become a fresh nickname form, even without clicking resume.
                    port = server.port
                    await server.close()
                    app = create_app(str(Path(temp) / 'browser.sqlite3'))
                    room = app[ROOM_KEY]
                    server = TestServer(app, port=port)
                    await server.start_server()
                    for page in pages:
                        await page.reload()
                        await page.locator('#join-form').wait_for(state='visible')
                        assert await page.locator('#resume-button').is_hidden()
                        assert await page.locator('#nickname').input_value() == ''
                        assert await page.evaluate("localStorage.getItem('tarot-seat-token')") is None
                    assert room.players == [None] * 4
                    # Context teardown also releases sockets from earlier page reloads.
                    await asyncio.gather(*(context.close() for context in contexts))
                    try:
                        async with asyncio.timeout(5):
                            while not room.paused:
                                await asyncio.sleep(0.03)
                    except TimeoutError:
                        print('Remaining sockets:', [[(ws.closed, str(ws.exception())) for ws in group] for group in room.connections])
                        for task in asyncio.all_tasks():
                            task.print_stack()
                        raise
                    assert not errors, errors
                    print(json.dumps({'browser': 'Microsoft Edge', 'players': 4, 'cards_played': played,
                                      'tricks': 18, 'refresh_recovered': True, 'mobile_overflow': False,
                                      'next_round': True, 'leave_and_resume': True,
                                      'outside_leave_releases_seat': True, 'fifth_browser_joined': True,
                                      'two_person_restart': True,
                                      'contract_auction': True, 'private_card_history': True,
                                      'music_transitions': True, 'countdown_tone': True,
                                      'quick_chats_and_gifts': True, 'all_award_popups': True,
                                      'vote_rejection_cooldown': True,
                                      'seat_history_links': True, 'ephemeral_table_chat': True,
                                      'avatar_gifts': True, 'ten_second_kitty_reveal': True,
                                      'service_restart_clears_cached_identity': True,
                                      'home_survives_reload_and_online': True, 'browser_errors': errors}, ensure_ascii=False))
                finally:
                    await browser.close()
        finally:
            await server.close()


if __name__ == '__main__':
    asyncio.run(main())
