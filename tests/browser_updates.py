"""Isolated real-Edge acceptance for player management, reveal and emotes."""
import asyncio
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from server import ROOM_KEY, create_app
from tests.browser_smoke import wait_state


async def main():
    with tempfile.TemporaryDirectory() as temp:
        app = create_app(str(Path(temp) / 'updates.sqlite3'))
        room = app[ROOM_KEY]
        room.bot_delay = 5
        server = TestServer(app)
        await server.start_server()
        errors = []
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(channel='msedge', headless=True)
                try:
                    contexts = [await browser.new_context(viewport={'width': 390 if i == 0 else 1440, 'height': 950}) for i in range(4)]
                    pages = [await c.new_page() for c in contexts]
                    names = ['名字特别长的塔罗牌玩家昵称甲', '名字特别长的塔罗牌玩家昵称乙', '名字特别长的塔罗牌玩家昵称丙', '名字特别长的塔罗牌玩家昵称丁']
                    for page, name in zip(pages, names):
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        await page.goto(str(server.make_url('/')))
                        await page.locator('#nickname').fill(name)
                        await page.locator('#join-button').click()
                        await page.locator('#game').wait_for(state='visible')
                    async def action(seat, action, payload=None):
                        await wait_state(pages[seat], '!pending')
                        revision = await pages[seat].evaluate('state.revision')
                        await pages[seat].evaluate('([a,p]) => send(a,p)', [action, payload or {}])
                        await wait_state(pages[seat], f'!pending && state.revision > {revision}')
                    await action(0, 'start')
                    taker = room.game.data['turn']
                    await action(taker, 'bid', {'bid': 'prise'})
                    while room.game.data['phase'] == 'bidding':
                        await action(room.game.data['turn'], 'bid', {'bid': 'pass'})
                    for page in pages:
                        await wait_state(page, 'state.phase === "discard"')
                        role = page.locator(f'[data-seat="{taker}"] .seat-role')
                        assert '庄家' in await role.inner_text()
                        assert await role.evaluate('(e) => {const r=e.getBoundingClientRect(); return r.width>0 && r.left>=0 && r.right<=innerWidth;}')
                    await pages[0].locator('#chat-open').click()
                    await pages[0].get_by_role('button', name='亮出表情 😂', exact=True).click()
                    for page in pages:
                        await page.locator('.emote-bubble').wait_for(state='visible')
                    Path('artifacts').mkdir(exist_ok=True)
                    await pages[0].screenshot(path='artifacts/updates-emote-mobile.png', full_page=True)
                    await pages[0].locator('.emote-bubble').wait_for(state='detached', timeout=6500)
                    await action(0, 'restart_vote', {'round': room.game.data['round']})
                    await action(1, 'restart_vote', {'poll_id': room.game.data['restart_poll']['id']})
                    await action(0, 'start')
                    for _ in range(4):
                        await action(room.game.data['turn'], 'bid', {'bid': 'pass'})
                    for page in pages:
                        await page.locator('#redeal-panel').wait_for(state='visible')
                        assert await page.locator('#redeal-hands .playing-card').count() == 78
                    assert await pages[0].evaluate('document.documentElement.scrollWidth <= innerWidth')
                    await pages[0].screenshot(path='artifacts/updates-all-pass-mobile.png', full_page=True)
                    await wait_state(pages[0], 'state.round === 2 && state.phase === "bidding"')
                    assert await pages[0].locator('#redeal-panel').is_hidden()
                    assert await pages[0].evaluate('state.revealed_hands.length === 0 && state.kitty.length === 0')
                    twin = await contexts[2].new_page()
                    await twin.goto(str(server.make_url('/')))
                    await twin.locator('#game').wait_for(state='visible')
                    await pages[0].get_by_role('button', name=f'移出 {names[2]}', exact=True).click()
                    for page in (pages[2], twin):
                        await page.locator('#join-form').wait_for(state='visible')
                        assert await page.locator('#resume-button').is_hidden()
                    assert room.is_bot(2)
                    await wait_state(pages[0], '!pending')
                    await action(0, 'restart_vote', {'round': room.game.data['round']})
                    await action(1, 'restart_vote', {'poll_id': room.game.data['restart_poll']['id']})
                    await pages[0].get_by_role('button', name=f'移出 {names[1]}', exact=True).click()
                    await pages[1].locator('#join-form').wait_for(state='visible')
                    assert room.players[1] is None
                    await pages[1].locator('#nickname').fill('新终端')
                    await pages[1].locator('#join-button').click()
                    await pages[1].locator('#game').wait_for(state='visible')
                    assert await pages[1].evaluate('state.seat') == 1
                    assert not errors, errors
                    print('PASS: long-name role visible, emote 5s, all-pass 78 cards/10s/privacy, kick all tabs, bot takeover and replacement; no JS errors')
                    for context in contexts:
                        await context.close()
                finally:
                    await browser.close()
        finally:
            await server.close()


if __name__ == '__main__':
    asyncio.run(main())
