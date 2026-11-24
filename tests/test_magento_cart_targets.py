"""Real CDP target and external checkout regressions, without model calls."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import magento_cart_agent as agent
from tests.test_magento_cart_browser import local_shop, browser_config

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("selected_count,other_count", [(1, 0), (0, 1)])
def test_cart_tool_and_final_read_follow_selected_subdomain(tmp_path, monkeypatch, selected_count, other_count):
    """Ambiguous headless focus must not replace the agent's cart origin."""
    import browser_use

    # Exercise production tool registration without constructing a paid Agent.
    monkeypatch.setattr(browser_use, "Agent", lambda **kwargs: SimpleNamespace(**kwargs))

    async def scenario():
        config = browser_config(tmp_path)
        config.model, config.max_steps, config.timeout = "deepseek-flash", 5, 30
        config.screenshot_dir = tmp_path
        url = "https://shop.test/"
        async with agent.launch_browser(config) as (context, bridge, endpoint):
            await context.route("https://**.shop.test/**", lambda route: route.fulfill(body="<h1>Checkout</h1>"))
            await context.route("https://shop.test/**", lambda route: route.fulfill(body="<h1>Catalog</h1>"))
            selected = await context.new_page()
            await selected.goto("https://checkout.shop.test/checkout")
            other = await context.new_page()
            await other.goto(url + "catalog")
            for page, count in ((selected, selected_count), (other, other_count)):
                await page.evaluate("count => localStorage.setItem('mage-cache-storage', JSON.stringify({cart:{summary_count:count}}))", count)
                # Deterministically reproduce Chromium's ambiguous headless focus.
                await page.evaluate("Object.defineProperty(document, 'hasFocus', {value: () => true})")
            await bridge.start()
            await select_agent_tab(context, bridge, selected)
            monkeypatch.setattr(agent, "CURRENT_URL", url)
            monkeypatch.setattr(agent, "CDP_URL", endpoint)
            capture = agent.CheckoutCapture(endpoint, url, tmp_path, 1, bridge)
            model = agent.build_agent(config, bridge, url, capture)
            tool = next(t for t in model.tools.functions if t.name == "verify_magento_cart")
            tool_cart = json.loads(await tool.function())
            result = agent.new_result(url)
            result.update(agent_platform="magento", agent_checkout_reached=True)
            observer = agent.ShopObserver(context, url, bridge)
            observer.capture = capture
            if selected_count:
                preview = next(t for t in model.tools.functions if t.name == "preview_checkout_screenshot")
                await preview.function()
                result["agent_screenshot_id"] = capture.receipt["screenshot_id"]
            await agent.finalize_url(result, config, endpoint, 1, observer)
            for cart in (tool_cart, result["cart"]):
                assert cart["source_url"] == selected.url
                assert cart["summary_count"] == selected_count
                assert cart["in_cart"] is bool(selected_count)
            assert result["cart_verified"] is bool(selected_count)
            assert result["status"] == ("success" if selected_count else "cart_failed")

    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable", [False, True])
def test_cart_target_fallback_keeps_foreign_checkout_out_of_scope(local_shop, tmp_path, monkeypatch, unavailable):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, bridge, endpoint):
            shop = await context.new_page()
            await shop.goto(url)
            foreign = await context.new_page()
            await foreign.goto(f"http://localhost:{local_shop}/checkout")
            await foreign.evaluate("localStorage.setItem('mage-cache-storage', JSON.stringify({cart:{summary_count:99}}))")
            await bridge.start()
            await select_agent_tab(context, bridge, foreign)
            if unavailable:
                from unittest.mock import AsyncMock
                monkeypatch.setattr(agent, "agent_target_page", AsyncMock(side_effect=RuntimeError("daemon unavailable")))
            monkeypatch.setattr(agent, "CURRENT_URL", url)
            monkeypatch.setattr(agent, "CDP_URL", endpoint)
            _, tool = agent.make_tools(bridge)
            result = agent.new_result(url)
            await agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), endpoint, 1,
                                     agent.ShopObserver(context, url, bridge))
            for cart in (json.loads(await tool()), result["cart"]):
                assert not cart["in_cart"]
                assert cart["source_url"] == shop.url

    asyncio.run(scenario())


async def select_agent_tab(context, bridge, page):
    """Select through the same daemon session API used by pinned Browser-Use."""
    from browser_use.browser.browser import _daemon_cdp, _daemon_request

    session = await context.new_cdp_session(page)
    try:
        target = (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
    finally:
        await session.detach()

    def select():
        attached = _daemon_cdp(bridge.name, "Target.attachToTarget", targetId=target, flatten=True)
        _daemon_request(bridge.name, {"meta": "set_session", "session_id": attached["sessionId"],
                                      "target_id": target})
    await asyncio.to_thread(select)
    await page.bring_to_front()


def test_observer_tracks_selected_target_without_deciding_login_ui(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, bridge, endpoint):
            observer = agent.ShopObserver(context, url)
            observer.bridge = bridge
            checkout = await context.new_page()
            await checkout.goto(url + "/checkout")
            login = await context.new_page()
            await login.goto(url + "/account/login")
            await login.set_content('<h1>Sign in to continue</h1><input type="password">')
            await bridge.start()
            await select_agent_tab(context, bridge, checkout)
            assert not (await observer.inspect())["blocker"], "optional login must not block guest checkout"
            assert observer.active_page is checkout
            await select_agent_tab(context, bridge, login)
            assert not (await observer.inspect())["blocker"], "the AI must interpret login UI"
            assert observer.active_page is login
    asyncio.run(scenario())


@pytest.mark.parametrize("http_block", [False, True])
def test_external_checkout_cloudflare_is_blocked(local_shop, tmp_path, http_block):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, bridge, endpoint):
            observer = agent.ShopObserver(context, url)
            page = await context.new_page()
            await page.goto(url)
            await page.get_by_role("button", name="Add to Cart").click()
            external = await context.new_page()
            await external.route("**/*", lambda route: route.fulfill(
                status=403 if http_block else 200,
                headers={"server": "cloudflare"} if http_block else {},
                body="Forbidden" if http_block else '<title>Just a moment...</title><div id="challenge-form">Cloudflare</div>'))
            await external.goto(f"http://localhost:{local_shop}/checkout")
            result = agent.new_result(url)
            await agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), endpoint, 1, observer)
            assert result["status"] == "blocked" and result["cloudflare"]
            assert result["cart"]["summary_count"] == 1, "external checkout must not change cart scope"
            assert not result["screenshot"]
    asyncio.run(scenario())


def test_old_unrelated_external_challenge_is_ignored(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, bridge, endpoint):
            old = await context.new_page()
            await old.goto(f"http://localhost:{local_shop}/checkout")
            await old.set_content('<title>Just a moment...</title><div id="challenge-form">Cloudflare</div>')
            observer = agent.ShopObserver(context, url)
            page = await context.new_page()
            await page.goto(url + "/checkout")
            assert not (await observer.inspect())["blocker"]
    asyncio.run(scenario())


def test_external_checkout_remains_related_after_challenge_navigation(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, bridge, endpoint):
            observer = agent.ShopObserver(context, url)
            page = await context.new_page()
            await page.goto(url)
            external = await context.new_page()
            await external.goto(f"http://localhost:{local_shop}/checkout")
            await external.goto(f"http://localhost:{local_shop}/blocked")
            assert (await observer.inspect())["cloudflare"], "challenge URL need not contain checkout"
    asyncio.run(scenario())


def test_external_marketing_cloudflare_does_not_block_the_shop(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, bridge, endpoint):
            observer = agent.ShopObserver(context, url, bridge)
            shop = await context.new_page()
            observer.initial_page = shop
            await shop.goto(url)
            await shop.get_by_role("button", name="Add to Cart").click()
            await bridge.start()
            await select_agent_tab(context, bridge, shop)
            assert not (await observer.inspect())["blocker"]
            marketing = await context.new_page()
            await select_agent_tab(context, bridge, marketing)
            await observer.inspect()
            await marketing.route("**/*", lambda route: route.fulfill(
                status=403, headers={"server": "cloudflare"},
                body='<title>Attention Required! | Cloudflare</title><div id="challenge-form">Cloudflare</div>'))
            await marketing.goto(f"http://localhost:{local_shop}/agency-information")
            observed = await observer.inspect()
            assert not observed["cloudflare"] and not observed["blocker"]

    asyncio.run(scenario())


def test_same_tab_checkout_redirect_to_external_challenge_is_blocked(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, bridge, endpoint):
            observer = agent.ShopObserver(context, url, bridge)
            page = await context.new_page()
            observer.initial_page = page
            await page.goto(url + "/checkout")
            await bridge.start()
            await select_agent_tab(context, bridge, page)
            await observer.inspect()
            await page.route("**/blocked", lambda route: route.fulfill(
                status=403, headers={"server": "cloudflare"}, body="Forbidden"))
            await page.goto(f"http://localhost:{local_shop}/blocked")
            assert (await observer.inspect())["cloudflare"]

    asyncio.run(scenario())
