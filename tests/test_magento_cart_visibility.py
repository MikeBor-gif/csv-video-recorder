"""Capture preserves UI prepared by AI; these tests do not simulate an LLM verdict."""
import asyncio
from pathlib import Path

import pytest

import magento_cart_agent as agent
from tests.test_magento_cart_browser import browser_config, local_shop

pytestmark = pytest.mark.browser


PAGE_STATE = """() => {
  const copy = document.body.cloneNode(true);
  // Playwright temporarily hides carets during screenshot and restores styles;
  // an empty style attribute has no effect on layout or the original UI.
  for (const el of [copy, ...copy.querySelectorAll('[style]')]) {
    if (el.hasAttribute('style') && !el.getAttribute('style').trim()) el.removeAttribute('style');
  }
  return {html:copy.innerHTML, x:scrollX, y:scrollY};
}"""


@pytest.mark.parametrize("markup", [
    '<div><h1>Adresse de livraison</h1><input aria-label="Adresse"><input aria-label="Ville"></div>',
    '<form><h1>Shipping address</h1><fieldset><input name="firstname"></fieldset><fieldset><input name="street"></fieldset></form>',
    '<style>input{display:block;margin:350px 0}</style><form><h1>Delivery</h1><input name="address"><input name="city"></form>',
])
def test_capture_preserves_arbitrary_layout_and_agent_scroll(local_shop, tmp_path, markup):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, _, endpoint):
            page = await context.new_page()
            await page.goto(url + "/checkout")
            await page.set_content(markup)
            await page.locator("input").last.scroll_into_view_if_needed()
            before = await page.evaluate(PAGE_STATE)
            screenshot, selected = await agent.capture_checkout(endpoint, url, tmp_path, 1)
            assert screenshot and Path(screenshot).is_file()
            assert selected == url + "/checkout"
            assert await page.locator("input").evaluate_all("els => els.every(el => el.value === '')")
            assert await page.evaluate(PAGE_STATE) == before
    asyncio.run(scenario())


def test_checkout_iframe_is_captured_without_python_form_matching(local_shop, tmp_path):
    async def scenario():
        url = f"http://127.0.0.1:{local_shop}"
        async with agent.launch_browser(browser_config(tmp_path)) as (context, _, endpoint):
            page = await context.new_page()
            await page.goto(url + "/checkout")
            await page.set_content('<iframe src="' + url + '/checkout" style="height:450px;width:100%"></iframe>')
            await page.wait_for_function("document.querySelector('iframe')?.contentDocument?.querySelector('input')")
            screenshot, _ = await agent.capture_checkout(endpoint, url, tmp_path, 1)
            assert screenshot and Path(screenshot).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            assert await page.evaluate("() => [...document.querySelector('iframe').contentDocument.querySelectorAll('input')].every(el => el.value === '')")
    asyncio.run(scenario())
