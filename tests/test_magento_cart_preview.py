"""The agent reviews the exact script-captured artifact; no UI classifier in Python."""
import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import magento_cart_agent as agent
from tests.test_magento_cart_browser import browser_config, local_shop
from tests.test_magento_cart_targets import select_agent_tab


@pytest.mark.browser
@pytest.mark.parametrize("case", ["approved", "stale", "tampered", "visual_issue"])
def test_preview_image_acknowledgement_controls_delivered_png(local_shop, tmp_path, case):
    from pydantic_ai import BinaryContent

    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, bridge, endpoint):
            page = await context.new_page()
            await page.goto(url)
            await page.get_by_role("button", name="Add to Cart").click()
            await page.goto(url + "/checkout")
            await page.evaluate("document.body.style.minHeight='1800px'")
            await bridge.start()
            await select_agent_tab(context, bridge, page)
            capture = agent.CheckoutCapture(endpoint, url, tmp_path, 1, bridge)
            tools, _ = agent.make_tools(bridge, capture)
            preview = next(t for t in tools.functions if t.name == "preview_checkout_screenshot")
            response = await preview.function(full_page=True)
            image = next(item for item in response.content if isinstance(item, BinaryContent))
            receipt = dict(response.return_value)
            assert image.media_type == "image/png"
            assert image.data == Path(receipt["screenshot"]).read_bytes()
            assert hashlib.sha256(image.data).hexdigest() == receipt["screenshot_id"]
            if case == "stale":
                # A second real capture supersedes the first image the model approved.
                await preview.function(full_page=False)
                assert capture.receipt["screenshot_id"] != receipt["screenshot_id"]
            elif case == "tampered":
                Path(receipt["screenshot"]).write_bytes(image.data + b"changed-after-review")
            result = agent.new_result(url)
            result.update(agent_platform="magento", agent_checkout_reached=True,
                          agent_screenshot_id=receipt["screenshot_id"])
            if case == "visual_issue":
                result["agent_visual_issues"] = ["A closable debug widget remains visible"]
            observer = agent.ShopObserver(context, url, bridge)
            observer.capture = capture
            await agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), endpoint, 1, observer)
            assert result["status"] == {"approved": "success", "stale": "checkout_not_reached",
                                        "tampered": "screenshot_failed",
                                        "visual_issue": "checkout_not_reached"}[case]
            if case in ("approved", "visual_issue"):
                assert result["screenshot_reviewed"]
                assert Path(result["screenshot"]).read_bytes() == image.data
                assert await page.locator("input").evaluate_all("els => els.every(el => el.value === '')")
            else:
                assert not result["screenshot_reviewed"]

    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("change", ["navigate", "switch_target", "reload", "fragment"])
def test_preview_cannot_approve_a_different_checkout_document(local_shop, tmp_path, change):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, bridge, endpoint):
            page = await context.new_page()
            await page.goto(url + "/checkout")
            await page.evaluate("localStorage.setItem('mage-cache-storage', JSON.stringify({cart:{summary_count:1}}))")
            await bridge.start()
            await select_agent_tab(context, bridge, page)
            capture = agent.CheckoutCapture(endpoint, url, tmp_path, 1, bridge)
            response = await capture.preview()
            image_id = response.return_value["screenshot_id"]
            if change == "navigate":
                await page.goto(url + "/catalog")
            elif change == "switch_target":
                other = await context.new_page()
                await other.goto(url + "/catalog")
                await select_agent_tab(context, bridge, other)
            elif change == "reload":
                await page.reload()
            else:
                await page.evaluate("location.hash='payment'")
            result = agent.new_result(url)
            result.update(agent_platform="magento", agent_checkout_reached=True, agent_screenshot_id=image_id)
            observer = agent.ShopObserver(context, url, bridge)
            observer.capture = capture
            await agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), endpoint, 1, observer)
            assert result["status"] == "checkout_not_reached"
            assert result["cart_verified"]
            assert not result["checkout_reached"] and not result["screenshot_current"]
            assert not result["screenshot_reviewed"]

    asyncio.run(scenario())


@pytest.mark.browser
def test_navigation_during_capture_does_not_create_a_receipt(local_shop, tmp_path, monkeypatch):
    from playwright.async_api import Page
    original = Page.screenshot

    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, bridge, endpoint):
            page = await context.new_page()
            await page.goto(url + "/checkout")
            await page.evaluate("localStorage.setItem('mage-cache-storage', JSON.stringify({cart:{summary_count:1}}))")
            await bridge.start()
            await select_agent_tab(context, bridge, page)

            async def capture_then_navigate(self, *args, **kwargs):
                data = await original(self, *args, **kwargs)
                await self.goto(url + "/catalog")
                return data

            monkeypatch.setattr(Page, "screenshot", capture_then_navigate)
            capture = agent.CheckoutCapture(endpoint, url, tmp_path, 1, bridge)
            response = await capture.preview()
            assert capture.receipt is None
            assert response.return_value["error"] == "preview_failed"

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["timeout", "error"])
def test_screenshot_failure_preserves_the_primary_error(tmp_path, monkeypatch, status):
    result = agent.new_result("https://shop.test")
    result.update(agent_platform="magento", agent_status=status, error="Original agent failure")
    monkeypatch.setattr(agent, "read_cart", AsyncMock(return_value=agent.normalize_cart(
        {"found": True, "cart": {"summary_count": 1}})))
    monkeypatch.setattr(agent, "capture_checkout", AsyncMock(side_effect=PermissionError("Screenshot directory denied")))
    asyncio.run(agent.finalize_url(result, SimpleNamespace(screenshot_dir=tmp_path), "endpoint", 1,
                                  SimpleNamespace(inspect=AsyncMock(return_value={}))))
    assert result["status"] == status
    assert result["error"] == "Original agent failure"
    assert result["screenshot_error"] == "Screenshot directory denied"


def test_failed_new_preview_invalidates_previous_image(tmp_path, monkeypatch):
    capture = agent.CheckoutCapture("endpoint", "https://shop.test", tmp_path, 1, None)
    capture.receipt = {"screenshot_id": "old-image"}
    monkeypatch.setattr(agent, "read_cart", AsyncMock(return_value=agent.normalize_cart({})))
    response = asyncio.run(capture.preview())
    assert response.return_value["error"] == "cart_not_verified"
    assert capture.receipt is None


def test_preview_tool_delivers_native_image_through_pinned_runtime(tmp_path, monkeypatch):
    from pydantic_ai import Agent, BinaryContent
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.messages import UserPromptPart

    path = tmp_path / "checkout.png"
    data = b"\x89PNG\r\n\x1a\nactual-capture-bytes"
    path.write_bytes(data)
    monkeypatch.setattr(agent, "read_cart", AsyncMock(return_value=agent.normalize_cart(
        {"found": True, "cart": {"summary_count": 1}})))
    async def capture_image(*args, **kwargs):
        kwargs["binding"].update(target_id="target", frame_id="frame", loader_id="loader",
                                 url="https://shop.test/checkout", fragment="")
        return str(path), "https://shop.test/checkout"

    monkeypatch.setattr(agent, "capture_checkout", capture_image)
    capture = agent.CheckoutCapture("endpoint", "https://shop.test", tmp_path, 1, None)
    tools, _ = agent.make_tools(capture=capture)

    async def scenario():
        model = TestModel(call_tools=["preview_checkout_screenshot"], custom_output_text="Reviewed")
        result = await Agent(model, tools=tools.functions).run("Inspect the checkout image")
        images = [item for message in result.all_messages() for part in message.parts
                  if isinstance(part, UserPromptPart) and isinstance(part.content, list)
                  for item in part.content if isinstance(item, BinaryContent)]
        assert len(images) == 1 and images[0].data == data

    asyncio.run(scenario())
