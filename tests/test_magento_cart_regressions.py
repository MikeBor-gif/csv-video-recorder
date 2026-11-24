"""Data/schema regressions retained after UI interpretation moved to the agent."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import magento_cart_agent as agent
from tests.test_magento_cart_browser import browser_config, local_shop


@pytest.mark.parametrize("quantity", [0.5, "0.25", 1.5])
def test_fractional_cart_quantity_is_preserved(quantity):
    result = agent.normalize_cart({"found": True, "cart": {"summary_count": quantity}})
    assert result["in_cart"]
    assert result["summary_count"] == float(quantity)


@pytest.mark.browser
def test_run_url_keeps_structured_agent_platform_and_evidence(local_shop, tmp_path, monkeypatch):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        config = browser_config(tmp_path)
        config.screenshot_dir = tmp_path
        history = SimpleNamespace(status="completed", success=False, error=None,
            structured_output=agent.AgentOutcome(platform="sylius", platform_evidence=["Observed storefront metadata"], outcome="Non-Magento"),
            final_result=lambda: json.dumps({"platform": "sylius", "outcome": "Non-Magento"}))

        async def run(model, config, observer):
            await observer.initial_page.set_content('<h1>Storefront with unfamiliar markup</h1>')
            return history

        monkeypatch.setattr(agent, "build_agent", lambda *args: object())
        monkeypatch.setattr(agent, "run_observed", run)
        result = await agent.run_url(config, url, 1)
        assert result["platform"] == "sylius"
        assert result["platform_evidence"] == ["Observed storefront metadata"]
        assert result["status"] == "platform_mismatch"
        assert not result["cart_verified"]
        assert result["agent_checkout_reached"] is False
    asyncio.run(scenario())


@pytest.mark.browser
def test_independent_magento_cart_overrides_conflicting_model_platform(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, _, endpoint):
            page = await context.new_page()
            await page.goto(url)
            await page.evaluate("localStorage.setItem('mage-cache-storage', JSON.stringify({cart:{summary_count:1}}))")
            result = agent.new_result(url)
            result.update(agent_platform="shopify", agent_platform_evidence=["Model inference"])
            await agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), endpoint, 1, agent.ShopObserver(context, url))
            assert result["platform"] == "magento"
            assert result["platform_evidence"] == ["mage-cache-storage.cart"]
            assert result["cart_verified"]
    asyncio.run(scenario())


def test_agent_output_schema_is_supported_by_pinned_runtime():
    from pydantic import TypeAdapter

    adapter = TypeAdapter(agent.AgentOutcome)
    outcome = adapter.validate_json('{"platform":"sylius","platform_evidence":["Sylius"],"outcome":"Non-Magento"}')
    assert outcome.platform == "sylius"
    assert not outcome.checkout_reached and outcome.blocker is None
    assert adapter.json_schema()["type"] == "object"
