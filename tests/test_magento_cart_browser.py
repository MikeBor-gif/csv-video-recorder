"""Real Chromium + pinned Browser-Use CDP tests; never instantiate a paid Agent."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import tempfile
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

import magento_cart_agent as agent

pytestmark = pytest.mark.browser


@pytest.fixture
def local_shop():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/blocked":
                content = '<title>Just a moment...</title><div id="challenge-form">Cloudflare</div>'
            elif self.path == "/shopify":
                content = '<script>window.Shopify={};</script>Powered by Shopify'
            elif self.path == "/checkout":
                content = '''<h1>Checkout</h1><form><h2>Shipping address</h2>
                <label>First name<input name="firstname" autocomplete="given-name"></label>
                <label>Street<input name="street" autocomplete="street-address"></label>
                <label>ZIP<input name="postcode" autocomplete="postal-code"></label>
                <label>City<input name="city" autocomplete="address-level2"></label>
                <label>Country<select name="country"><option>Germany</option></select></label></form>'''
            else:
                content = '''<h1>Local Magento product</h1>
                <button onclick="localStorage.setItem('mage-cache-storage', JSON.stringify({cart:{summary_count:1,items:[{product_sku:'TEST'}],subtotalAmount:19.99}}))">Add to Cart</button>
                <a href="/checkout">Checkout</a>'''
            body = ("<html><body>" + content + "</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html;charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port
    server.shutdown()
    server.server_close()
    thread.join()


def browser_config(tmp_path, persistent=False):
    return SimpleNamespace(headless=True, profile_dir=str(tmp_path / "profile") if persistent else None,
                           cdp_port=0, channel=os.getenv("MAGENTO_TEST_CHANNEL"), executable_path=None,
                           user_agent=None, proxy=None, stealth_js=None, cookies=None)


def test_shared_cdp_tool_screenshot_and_cleanup(local_shop, tmp_path, monkeypatch):
    """Two drivers share cookies/storage; a foreign old tab cannot verify a cart."""
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_AUTO_LOGIN", "false")
    original_mkdtemp = tempfile.mkdtemp
    created = []

    def record_profile(*args, **kwargs):
        path = original_mkdtemp(*args, **kwargs)
        if kwargs.get("prefix") == "magento-agent-":
            created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", record_profile)

    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, bridge, endpoint):
            page = await context.new_page()
            await page.goto(url)
            await context.add_cookies([{"name": "shared", "value": "yes", "url": url}])
            await bridge.start()  # Actual pinned Browser-Use bridge, no model/API.
            async with agent.cdp_reader(endpoint) as reader:
                assert len(reader.contexts) == 1
                assert any(c["name"] == "shared" for c in await reader.contexts[0].cookies(url))
            assert not page.is_closed(), "reader disconnect must not close owner"
            monkeypatch.setattr(agent, "CURRENT_URL", url)
            monkeypatch.setattr(agent, "CDP_URL", endpoint)
            tools, tool = agent.make_tools()
            registered = next(t for t in tools.functions if t.name == "verify_magento_cart")
            assert registered.function is tool
            assert not json.loads(await registered.function())["in_cart"]
            await page.get_by_role("button", name="Add to Cart").click()
            assert json.loads(await registered.function())["in_cart"]
            # Read must refresh state, rather than caching the earlier tool answer.
            await page.evaluate("localStorage.setItem('mage-cache-storage', JSON.stringify({cart:{summary_count:0}}))")
            assert not (await agent.read_cart(endpoint, url))["in_cart"]
            await page.get_by_role("button", name="Add to Cart").click()
            foreign = await context.new_page()
            await foreign.goto(f"http://localhost:{local_shop}/checkout")
            await foreign.evaluate("localStorage.setItem('mage-cache-storage', JSON.stringify({cart:{summary_count:99}}))")
            await page.evaluate("localStorage.removeItem('mage-cache-storage')")
            assert not (await agent.read_cart(endpoint, url))["in_cart"]
            # PDF §8 allows another checkout host as fallback; only the cart
            # read above is restricted to the current host (§4).
            screenshot, selected = await agent.capture_checkout(endpoint, url, tmp_path, 1)
            assert Path(screenshot).is_file() and urlsplit(selected).hostname == "localhost"
            await page.goto(url + "/checkout")
            screenshot, checkout_url = await agent.capture_checkout(endpoint, url, tmp_path, 1)
            assert Path(screenshot).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            assert checkout_url.endswith("/checkout")
            assert await page.locator("input").evaluate_all("els => els.every(e => e.value === '')")
        assert created and all(not path.exists() for path in created)

    asyncio.run(scenario())


def test_platform_blocker_and_persistent_profile(local_shop, tmp_path):
    async def scenario():
        config = browser_config(tmp_path, persistent=True)
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(config) as (context, _, endpoint):
            observer = agent.ShopObserver(context, url)
            page = await context.new_page()
            await page.goto(url + "/shopify")
            assert (await observer.inspect())["platform"] == "shopify"
            await page.goto(url + "/blocked")
            assert (await observer.inspect())["cloudflare"]
            assert (await observer.inspect())["blocker"] == "blocked by Cloudflare"
        assert Path(config.profile_dir).is_dir()

    asyncio.run(scenario())


def test_account_address_form_is_not_checkout(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, _, endpoint):
            page = await context.new_page()
            await page.goto(url + "/account/address")
            await page.set_content('''<h1>Edit contact address</h1><form>
                <input name="firstname"><input name="street"><input name="postcode"><input name="city">
                </form>''')
            assert await agent.capture_checkout(endpoint, url, tmp_path, 1) == (None, None)
            assert not list(tmp_path.glob("*.png"))

    asyncio.run(scenario())


def test_diagnostic_capture_does_not_dismiss_consent_or_claim_readiness(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, _, endpoint):
            page = await context.new_page()
            await page.goto(url + "/checkout")
            await page.evaluate('''() => {
                const overlay = document.createElement('div'); overlay.id='consent';
                overlay.style='position:fixed;inset:0;z-index:2147483647;background:white';
                overlay.innerText='Please accept cookies'; document.body.append(overlay);
            }''')
            screenshot, _ = await agent.capture_checkout(endpoint, url, tmp_path, 1)
            assert Path(screenshot).is_file(), "PDF §8 captures the page; AI decides UI readiness"
            assert await page.locator("#consent").is_visible(), "capture must not change the UI"
            result = agent.new_result(url)
            await page.evaluate("localStorage.setItem('mage-cache-storage', JSON.stringify({cart:{summary_count:1}}))")
            await agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), endpoint, 2, agent.ShopObserver(context, url))
            assert result["screenshot"] and result["status"] == "checkout_not_reached"
            assert not result["checkout_reached"], "a PNG is not an agent UI verdict"
            await page.locator("#consent").evaluate("el => el.remove()")
            screenshot, _ = await agent.capture_checkout(endpoint, url, tmp_path, 1)
            assert Path(screenshot).is_file()
            assert await page.locator("input").evaluate_all("els => els.every(el => el.value === '')")

    asyncio.run(scenario())


def test_screenshot_prefers_current_site_and_allows_checkout_fallback(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        foreign = f"http://localhost:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, _, endpoint):
            local = await context.new_page()
            await local.goto(url + "/checkout")
            other = await context.new_page()
            await other.goto(foreign + "/checkout")
            _, selected = await agent.capture_checkout(endpoint, url, tmp_path, 1)
            assert urlsplit(selected).hostname == "127.0.0.1"
            await local.close()
            _, selected = await agent.capture_checkout(endpoint, url, tmp_path, 1)
            assert selected and urlsplit(selected).hostname == "localhost"
            # §8 preference never relaxes the independent cart scope of §4.
            assert not (await agent.read_cart(endpoint, url))["in_cart"]

    asyncio.run(scenario())


def test_repeated_browser_launch_keeps_proxy_bypass_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("no_proxy", "example.test, localhost")
    monkeypatch.setenv("NO_PROXY", "example.test, localhost")

    async def scenario():
        async with agent.launch_browser(browser_config(tmp_path)):
            pass
        first = os.environ["no_proxy"]
        async with agent.launch_browser(browser_config(tmp_path)):
            pass
        assert os.environ["no_proxy"] == first
        assert os.environ["NO_PROXY"] == first

    asyncio.run(scenario())


def test_checkout_url_variants_and_long_shipping_form(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, _, endpoint):
            page = await context.new_page()
            for index, route in enumerate(("/onepage", "/kasse", "/cassa", "/checkout/#shipping"), 1):
                await page.goto(url + route)
                await page.set_content('''<style>input {display:block;margin:250px 0}</style>
                    <form><h1>Shipping address</h1><input name="firstname"><input name="street">
                    <input name="postcode"><input name="city"></form>''')
                screenshot, selected = await agent.capture_checkout(endpoint, url, tmp_path, index)
                assert screenshot and Path(screenshot).is_file()
                assert urlsplit(selected).path == urlsplit(url + route).path
                assert await page.locator("input").evaluate_all("els => els.every(el => el.value === '')")

    asyncio.run(scenario())


def test_observer_follows_real_focus_to_older_challenge_tab(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, _, endpoint):
            first = await context.new_page()
            await first.goto(url + "/blocked")
            later = await context.new_page()
            await later.goto(url)
            await first.bring_to_front()
            await first.wait_for_function("document.hasFocus()", timeout=3000)
            result = await agent.ShopObserver(context, url).inspect()
            states = [await item.evaluate("() => [document.hasFocus(), document.visibilityState, performance.timeOrigin]")
                      for item in (first, later)]
            assert result["cloudflare"] and result["blocker"] == "blocked by Cloudflare", states

    asyncio.run(scenario())
