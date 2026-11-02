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
                    await pages[room.host].get_by_role('button', name='开始游戏 →').click()
                    await wait_state(pages[0], "state.phase === 'bidding'")
                    deadline_before_vote = room.deadline
                    await pages[0].get_by_role('button', name='重开投票', exact=True).click()
                    await wait_state(pages[1], 'state.restart_votes.length === 1')
                    assert room.deadline == deadline_before_vote
                    assert await pages[0].get_by_role('button', name='已赞成（1/2）', exact=True).is_disabled()
                    await pages[0].reload()
                    await pages[0].locator('#game').wait_for(state='visible')
                    assert await pages[0].get_by_role('button', name='已赞成（1/2）', exact=True).is_disabled()
                    await pages[1].screenshot(path=str(artifacts / 'restart-vote.png'), full_page=True)
                    await pages[1].get_by_role('button', name='赞成重开（1/2）', exact=True).click()
                    await wait_state(pages[0], "state.round === 2 && state.phase === 'bidding'")
                    assert room.game.data.get('restart_votes', []) == []
                    assert all(p['total2'] == 0 for p in room.players)
                    bidder = room.game.data['turn']
                    await pages[bidder].get_by_role('button', name='Petit ×1', exact=True).click()
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
                    assert set(kitty) <= set(await pages[bidder].evaluate('state.hand'))
                    await pages[bidder].reload()
                    await pages[bidder].locator('#game').wait_for(state='visible')
                    assert await pages[bidder].evaluate('state.seat') == bidder
                    hand_before = await pages[bidder].evaluate('state.hand.slice()')
                    deadline_before = room.deadline
                    await pages[bidder].get_by_role('button', name='离开房间', exact=True).click()
                    await pages[bidder].locator('#welcome').wait_for(state='visible')
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
                    await wait_state(pages[0], "state.phase === 'bidding' && state.round === 3")
                    # Finish browser WebSocket handshakes before closing Edge's pipes on Windows.
                    await asyncio.gather(*(p.get_by_role('button', name='离开房间', exact=True).click() for p in pages))
                    await asyncio.gather(*(p.locator('#welcome').wait_for(state='visible') for p in pages))
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
                                      'home_survives_reload_and_online': True, 'browser_errors': errors}, ensure_ascii=False))
                finally:
                    await browser.close()
        finally:
            await server.close()


if __name__ == '__main__':
    asyncio.run(main())
