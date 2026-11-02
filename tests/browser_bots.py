"""Real Edge acceptance for bot controls and a complete human/bot game.

Run: .venv/Scripts/python.exe tests/browser_bots.py
Uses an independent temporary room; never touches the running game's database.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright

from server import ROOM_KEY, create_app
from tests.browser_smoke import wait_state


async def main():
    artifacts = Path(__file__).resolve().parents[1] / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    errors = []
    with tempfile.TemporaryDirectory() as temp:
        app = create_app(str(Path(temp) / 'bots.sqlite3'), {'bot': .03, 'collecting': .03})
        room = app[ROOM_KEY]
        server = TestServer(app)
        await server.start_server()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel='msedge', headless=True)
                try:
                    desktop = await browser.new_context(viewport={'width': 1440, 'height': 1050})
                    mobile = await browser.new_context(viewport={'width': 390, 'height': 844})
                    host, guest = await desktop.new_page(), await mobile.new_page()
                    for page in (host, guest):
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                    url = str(server.make_url('/'))
                    await host.goto(url)
                    await host.locator('#nickname').fill('独自练牌')
                    await host.locator('#join-button').click()
                    await host.locator('#bot-panel').wait_for(state='visible')
                    assert await host.locator('#bot-policy').input_value() == 'normal'
                    assert await host.locator('#bot-policy option').evaluate_all('(options) => options.map(option => option.value)') == ['normal', 'advanced']
                    await host.locator('#bot-policy').select_option('advanced')
                    await host.get_by_role('button', name='在第 4 号座位添加人机').click()
                    await wait_state(host, 'state.players[3]?.kind === "bot" && !pending')
                    assert room.players[3]['policy'] == 'advanced'
                    # Empty-seat controls must re-enable after the add acknowledgement.
                    assert await host.get_by_role('button', name='在第 2 号座位添加人机').is_enabled()
                    await host.locator('#bot-policy').select_option('normal')
                    await host.get_by_role('button', name='添加人机', exact=True).click()
                    await wait_state(host, 'state.players.filter(p => p?.kind === "bot").length === 2 && !pending')
                    await host.locator('#bot-policy').select_option('normal')
                    await host.get_by_role('button', name='补齐人机', exact=True).click()
                    await wait_state(host, 'state.players.every(Boolean) && !pending')
                    assert await host.locator('#bot-count').inner_text() == '1 真人 · 3 人机'
                    assert await host.get_by_role('button', name='补齐人机', exact=True).is_disabled()
                    assert await host.get_by_role('button', name='开始游戏 →', exact=True).is_enabled()
                    assert [p.get('policy') for p in room.players] == [None, 'normal', 'normal', 'advanced']
                    assert await host.locator('#bot-roster option[value="random"]').count() == 0
                    # Individual changes use the same host-only outside-game protocol.
                    bot_name = room.players[1]['name']
                    await host.get_by_role('combobox', name=f'{bot_name}的难度').select_option('advanced')
                    await wait_state(host, '!pending && state.players[1].bot_policy === "advanced"')
                    await host.get_by_role('combobox', name=f'{bot_name}的难度').select_option('normal')
                    await wait_state(host, '!pending && state.players[1].bot_policy === "normal"')
                    await host.screenshot(path=str(artifacts / 'bots-lobby-desktop.png'), full_page=True)
                    await host.set_viewport_size({'width': 390, 'height': 844})
                    assert await host.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    await host.screenshot(path=str(artifacts / 'bots-difficulty-mobile.png'), full_page=True)
                    await host.set_viewport_size({'width': 1440, 'height': 1050})

                    await host.get_by_role('button', name='开始游戏 →', exact=True).click()
                    await wait_state(host, '!pending && state.phase !== "lobby"')
                    assert await host.locator('#bot-panel').is_hidden()
                    await host.get_by_role('button', name='重新开局', exact=True).click()
                    await wait_state(host, 'state.phase === "lobby" && !pending')
                    assert sum(room.is_bot(i) for i in range(4)) == 3
                    await host.get_by_role('button', name='开始游戏 →', exact=True).click()
                    await wait_state(host, '!pending && state.phase !== "lobby"')
                    await host.get_by_role('button', name='离开房间', exact=True).click()
                    await host.locator('#welcome').wait_for(state='visible')
                    async with asyncio.timeout(3):
                        while not room.paused:
                            await asyncio.sleep(.02)
                    paused_game = deepcopy(room.game.data)
                    await asyncio.sleep(.6)
                    assert room.game.data == paused_game
                    assert await host.locator('#resume-button').is_visible()
                    await host.locator('#resume-button').click()
                    await host.locator('#game').wait_for(state='visible')

                    # All human actions use the actual controls; bots use server scheduling.
                    async with asyncio.timeout(75):
                        while True:
                            await wait_state(host, '!pending && (state.phase === "finished" || state.turn === state.seat)')
                            view = await host.evaluate('state')
                            if view['phase'] == 'finished':
                                break
                            if view['phase'] == 'bidding':
                                button = '不叫' if view['contract'] else 'Prise ×1'
                                await host.get_by_role('button', name=button, exact=True).click()
                            elif view['phase'] == 'discard':
                                # Use the engine's legal selection as input, then click all six cards.
                                from game.rules import auto_discard
                                for card in auto_discard(view['hand']):
                                    await host.locator(f'#hand [data-card="{card}"]').click()
                                    await wait_state(host, '!pending')
                                await host.get_by_role('button', name='确认扣六张', exact=True).click()
                            elif view['phase'] == 'playing':
                                await host.locator(f'#hand [data-card="{view["legal"][0]}"]').click()
                                await host.get_by_role('button', name='出牌 →', exact=True).click()
                            await wait_state(host, f'!pending && state.revision > {view["revision"]}')
                    assert len(room.game.data['tricks']) == 18
                    assert sum(room.game.data['result']['scores']) == 0
                    assert await host.locator('#result').is_visible()
                    await host.screenshot(path=str(artifacts / 'bots-result-desktop.png'), full_page=True)

                    # Release a bot in the finished phase; a real mobile player can join.
                    old_result = deepcopy(room.game.data['result'])
                    bot_name = room.players[1]['name']
                    await host.get_by_role('button', name=f'移除{bot_name}', exact=True).click()
                    await wait_state(host, '!pending && state.players[1] === null')
                    await guest.goto(url)
                    await guest.locator('#nickname').fill('手机好友')
                    await guest.locator('#join-button').click()
                    await guest.locator('#bot-panel').wait_for(state='visible')
                    await wait_state(host, 'state.players[1]?.kind === "human" && state.players[1].online')
                    assert await guest.locator('#bot-actions button').count() == 0
                    assert await guest.locator('#bot-roster button').count() == 0
                    assert await guest.locator('#bot-policy-label').is_hidden()
                    assert await guest.locator('#bot-roster select').count() == 0
                    assert await guest.locator('#bot-count').inner_text() == '2 真人 · 2 人机'
                    assert room.game.data['result'] == old_result
                    assert await guest.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    await guest.screenshot(path=str(artifacts / 'bots-lobby-mobile.png'), full_page=True)
                    await guest.reload()
                    await guest.locator('#game').wait_for(state='visible')
                    assert await guest.evaluate('state.seat') == 1

                    # Next game uses two human ballots, no votes from bots.
                    await host.get_by_role('button', name='开始下一局 →', exact=True).click()
                    await wait_state(host, '!pending && state.phase !== "finished"')
                    await host.get_by_role('button', name='重开投票', exact=True).click()
                    await wait_state(guest, 'state.restart_poll?.yes.length === 1')
                    assert '/2' in await guest.locator('#restart-status').inner_text()
                    await guest.get_by_role('button', name='同意重开', exact=True).click()
                    await wait_state(host, 'state.phase === "lobby" && !pending')
                    await wait_state(guest, 'state.phase === "lobby" && !pending')
                    # Last human leaving removes remaining bots and revokes identities.
                    await host.get_by_role('button', name='离开房间', exact=True).click()
                    await wait_state(guest, 'state.host === state.seat')
                    assert await guest.get_by_role('button', name='补齐人机', exact=True).is_enabled()
                    await guest.get_by_role('button', name='离开房间', exact=True).click()
                    await guest.locator('#join-form').wait_for(state='visible')
                    assert room.players == [None] * 4
                    assert not errors, errors
                    print(json.dumps({'full_round': True, 'add_remove_fill': True,
                                      'two_difficulties_and_individual_changes': True,
                                      'single_human_restart_pause_resume': True,
                                      'mobile_replacement_and_two_human_vote': True,
                                      'last_human_clears_bots': True, 'browser_errors': errors}))
                    await desktop.close()
                    await mobile.close()
                finally:
                    await browser.close()
        finally:
            await server.close()


if __name__ == '__main__':
    asyncio.run(main())
