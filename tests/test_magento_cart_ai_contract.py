"""Task 4 ownership: AI understands UI, Python verifies cart and captures PNG."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import magento_cart_agent as agent
from tests.test_magento_cart_browser import browser_config, local_shop
from tests.test_magento_cart_targets import select_agent_tab


def test_platform_from_agent_does_not_require_python_dom_classifier(tmp_path, monkeypatch):
    result = agent.new_result("https://shop.test")
    result.update(agent_platform="prestashop", agent_platform_evidence=["Identified from storefront scripts"])
    monkeypatch.setattr(agent, "read_cart", AsyncMock(return_value=agent.normalize_cart({})))
    observer = SimpleNamespace(inspect=AsyncMock(return_value=agent.classify_probe({})), confirm_platform=AsyncMock(return_value=[]))
    asyncio.run(agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), "endpoint", 1, observer))
    assert result["platform"] == "prestashop"
    assert result["status"] == "platform_mismatch"


@pytest.mark.parametrize("ready", [True, False])
def test_agent_checkout_readiness_is_distinct_from_saved_png(tmp_path, monkeypatch, ready):
    result = agent.new_result("https://shop.test")
    result.update(platform="magento", agent_checkout_reached=ready)
    monkeypatch.setattr(agent, "read_cart", AsyncMock(return_value=agent.normalize_cart({"found": True, "cart": {"summary_count": 1}})))
    monkeypatch.setattr(agent, "capture_checkout", AsyncMock(return_value=("checkout.png", "https://shop.test/checkout")))
    observer = SimpleNamespace(inspect=AsyncMock(return_value={}))
    asyncio.run(agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), "endpoint", 1, observer))
    assert result["cart_verified"]
    assert result["screenshot"] == "checkout.png"
    assert not result["checkout_reached"], "a readiness claim must acknowledge the actual PNG"
    assert result["status"] == "checkout_not_reached"


def test_model_cannot_replace_independent_cart_read(tmp_path, monkeypatch):
    result = agent.new_result("https://shop.test")
    result.update(platform="magento", agent_success=True, agent_checkout_reached=True)
    monkeypatch.setattr(agent, "read_cart", AsyncMock(return_value=agent.normalize_cart({"found": True, "cart": {"summary_count": 0}})))
    capture = AsyncMock()
    monkeypatch.setattr(agent, "capture_checkout", capture)
    asyncio.run(agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), "endpoint", 1,
                                  SimpleNamespace(inspect=AsyncMock(return_value={}))))
    assert result["status"] == "cart_failed"
    assert not result["cart_verified"]
    capture.assert_not_awaited()


def test_unseen_checkout_png_cannot_be_reported_as_success(tmp_path, monkeypatch):
    """A readiness claim does not acknowledge the actual delivered screenshot."""
    result = agent.new_result("https://shop.test")
    result.update(agent_platform="magento", agent_success=True,
                  agent_checkout_reached=True, agent_screenshot_id="unseen-image")
    monkeypatch.setattr(agent, "read_cart", AsyncMock(return_value=agent.normalize_cart(
        {"found": True, "cart": {"summary_count": 1}})))
    monkeypatch.setattr(agent, "capture_checkout", AsyncMock(return_value=("checkout.png", "https://shop.test/checkout")))
    asyncio.run(agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), "endpoint", 1,
                                  SimpleNamespace(inspect=AsyncMock(return_value={}))))
    assert result["cart_verified"]
    assert result["screenshot"] == "checkout.png"
    assert result["status"] == "checkout_not_reached"
    assert not result["checkout_reached"]


@pytest.mark.parametrize("blocker", ["captcha", "login_required"])
def test_ui_blocker_is_reported_by_agent(tmp_path, monkeypatch, blocker):
    result = agent.new_result("https://shop.test")
    result.update(platform="magento", agent_blocker=blocker, agent_blocker_evidence=["Observed mandatory challenge"])
    monkeypatch.setattr(agent, "read_cart", AsyncMock(return_value=agent.normalize_cart({"found": True, "cart": {"summary_count": 1}})))
    capture = AsyncMock()
    monkeypatch.setattr(agent, "capture_checkout", capture)
    asyncio.run(agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), "endpoint", 1,
                                  SimpleNamespace(inspect=AsyncMock(return_value={}))))
    assert result["status"] == "blocked" and not result["cloudflare"]
    assert result["blocker"]
    capture.assert_not_awaited()


@pytest.mark.browser
def test_script_captures_checkout_without_interpreting_ui(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, _, endpoint):
            page = await context.new_page()
            await page.goto(url + "/checkout")
            await page.set_content('<shipping-ui></shipping-ui>')
            await page.locator('shipping-ui').evaluate("el => el.attachShadow({mode:'open'}).innerHTML='<h1>Delivery</h1><input aria-label=Address>'")
            screenshot, selected = await agent.capture_checkout(endpoint, url, tmp_path, 1)
            assert screenshot and Path(screenshot).is_file()
            assert selected == url + "/checkout"
            assert await page.locator('shipping-ui input').input_value() == ''
    asyncio.run(scenario())


@pytest.mark.browser
def test_optional_login_ui_is_left_for_model_to_interpret(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, _, _):
            page = await context.new_page()
            await page.goto(url)
            await page.set_content('<aside>Sign in to continue<input type=password></aside><button>Continue as guest</button>')
            assert (await agent.ShopObserver(context, url).inspect())["blocker"] is None
    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("ready", [True, False])
def test_structured_agent_decision_is_carried_to_final_result(local_shop, tmp_path, monkeypatch, ready):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        config = browser_config(tmp_path)
        config.screenshot_dir = tmp_path
        outcome = agent.AgentOutcome(platform="magento", platform_evidence=["Observed storefront"],
                                     outcome="UI assessment by agent", checkout_reached=ready)
        history = SimpleNamespace(status="completed", success=True, error=None,
                                  structured_output=outcome, final_result=lambda: "UI assessment by agent")

        async def run(model, config, observer):
            page = observer.initial_page
            await page.get_by_role('button', name='Add to Cart').click()
            await page.goto(url + '/checkout')
            if ready:
                await observer.bridge.start()
                await select_agent_tab(observer.context, observer.bridge, page)
                await observer.capture.preview()
                outcome.screenshot_id = observer.capture.receipt["screenshot_id"]
            return history

        monkeypatch.setattr(agent, 'build_agent', lambda *args: object())
        monkeypatch.setattr(agent, 'run_observed', run)
        result = await agent.run_url(config, url, 1)
        assert result['agent_checkout_reached'] is ready
        assert result['checkout_reached'] is ready
        assert result['cart_verified']
        assert Path(result['screenshot']).is_file()
        assert result['status'] == ('success' if ready else 'checkout_not_reached')
    asyncio.run(scenario())


def test_observed_cloudflare_overrides_model_readiness(tmp_path, monkeypatch):
    result = agent.new_result('https://shop.test')
    result.update(agent_platform='magento', agent_checkout_reached=True, agent_success=True)
    monkeypatch.setattr(agent, 'read_cart', AsyncMock(return_value=agent.normalize_cart({'found': True, 'cart': {'summary_count': 1}})))
    capture = AsyncMock()
    monkeypatch.setattr(agent, 'capture_checkout', capture)
    observer = SimpleNamespace(inspect=AsyncMock(return_value=agent.classify_probe({'markers': ['challenge DOM']})))
    asyncio.run(agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), 'endpoint', 1, observer))
    assert result['status'] == 'blocked' and result['cloudflare']
    capture.assert_not_awaited()


@pytest.mark.browser
def test_screenshot_uses_page_selected_by_agent_not_old_cart(local_shop, tmp_path):
    async def scenario():
        url = f'http://127.0.0.1:{local_shop}'
        async with agent.launch_browser(browser_config(tmp_path)) as (context, bridge, endpoint):
            old_cart = await context.new_page()
            await old_cart.goto(url + '/checkout/cart/')
            checkout = await context.new_page()
            await checkout.goto(url + '/checkout')
            await checkout.evaluate("localStorage.setItem('mage-cache-storage', JSON.stringify({cart:{summary_count:1}}))")
            await bridge.start()
            await select_agent_tab(context, bridge, checkout)
            result = agent.new_result(url)
            result.update(agent_platform='magento', agent_checkout_reached=True)
            observer = agent.ShopObserver(context, url, bridge)
            observer.capture = agent.CheckoutCapture(endpoint, url, tmp_path, 1, bridge)
            await observer.capture.preview()
            result['agent_screenshot_id'] = observer.capture.receipt['screenshot_id']
            await agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), endpoint, 1, observer)
            assert result['status'] == 'success'
            assert result['checkout_url'] == url + '/checkout'
    asyncio.run(scenario())


def test_selected_checkout_is_independent_of_cdp_page_enumeration(tmp_path, monkeypatch):
    async def screenshot(path, **kwargs):
        Path(path).write_bytes(b'\x89PNG\r\n\x1a\n')

    old = SimpleNamespace(url='https://shop.test/checkout/cart/', is_closed=lambda: False,
                          screenshot=screenshot)
    selected = SimpleNamespace(url='https://shop.test/checkout/', is_closed=lambda: False,
                               screenshot=screenshot)
    context = SimpleNamespace(pages=[old, selected])

    @asynccontextmanager
    async def reader(endpoint):
        yield SimpleNamespace(contexts=[context])

    monkeypatch.setattr(agent, 'cdp_reader', reader)
    monkeypatch.setattr(agent, 'agent_target_page', AsyncMock(return_value=selected))
    monkeypatch.setattr(agent, 'read_cart', AsyncMock(return_value=agent.normalize_cart({'found': True, 'cart': {'summary_count': 1}})))
    result = agent.new_result('https://shop.test')
    result.update(agent_platform='magento', agent_checkout_reached=True)
    observer = SimpleNamespace(bridge=object(), inspect=AsyncMock(return_value={}))
    asyncio.run(agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), 'endpoint', 1, observer))
    assert result['checkout_url'] == selected.url
