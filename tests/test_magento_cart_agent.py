"""Offline Task 4 contract tests: no browser, database or paid model calls."""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import magento_cart_agent as agent


@pytest.fixture
def config(tmp_path):
    return SimpleNamespace(max_steps=3, timeout=.1, screenshot_dir=str(tmp_path),
                           output=str(tmp_path / "results.json"), stop_on_success=False)


@pytest.mark.parametrize("raw,expected", [
    ("shop.de", "https://shop.de"), ("http://shop.de/a", "http://shop.de/a"),
    (" https://shop.de/a?q=1 ", "https://shop.de/a?q=1"),
])
def test_url_normalization(raw, expected):
    assert agent.validate_url(raw) == expected


@pytest.mark.parametrize("raw", ["https://", "ftp://shop.de", "https://a:b@shop.de", "a b.de", "https://shop.de:bad"])
def test_invalid_url(raw):
    with pytest.raises(ValueError):
        agent.validate_url(raw)


def test_input_file_bom_and_samples(tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text("\ufeff# ignored\n\n shop.de \n # comment\nother.de\n", encoding="utf-8")
    assert agent.load_urls(path) == ["shop.de", "other.de"]
    assert len(agent.load_urls("urls.txt")) == 6


@pytest.mark.parametrize("page,allowed", [
    ("https://www.example.de/x", True), ("https://checkout.example.de/x", True),
    ("https://example.de.evil.test", False), ("https://evil-example.de", False),
    ("about:blank", False), ("https://other.de", False),
])
def test_host_scope(page, allowed):
    assert agent.host_matches(page, "https://example.de") is allowed


@pytest.mark.parametrize("snapshot,in_cart,count", [
    ({}, False, 0), ({"found": True, "cart": None}, False, 0),
    ({"found": True, "cart": {"summary_count": "2"}}, True, 2),
    ({"found": True, "cart": {"summary_count": "NaN"}}, False, 0),
    ({"found": True, "cart": {"summary_count": -1}}, False, 0),
    ({"found": True, "cart": {"items": [{"product_sku": "A"}]}}, True, 0),
    ({"found": True, "cart": {"items_count": "1"}}, True, 0),
    ({"reason": "storage_read_error:SyntaxError"}, False, 0),
])
def test_cart_truth(snapshot, in_cart, count):
    result = agent.normalize_cart(snapshot)
    assert result["in_cart"] is in_cart
    assert result["summary_count"] == count


def test_cart_skus_and_subtotal():
    cart = agent.normalize_cart({"found": True, "cart": {
        "items": [{"product_sku": "A"}, {"sku": "B"}, None], "subtotalAmount": 19.99}})
    assert cart["skus"] == ["A", "B"]
    assert cart["subtotal"] == 19.99


def test_environment_load_precedes_defaults_and_cli_wins(tmp_path, monkeypatch):
    pytest.importorskip("dotenv")
    for name in ("MAGENTO_MODEL", "DEEPSEEK_API_KEY", "MAGENTO_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / "private.env"
    env.write_text("DEEPSEEK_API_KEY=test-key\nMAGENTO_MODEL=from-file\nMAGENTO_TIMEOUT=12\n")
    first = agent.parse_config(["--env-file", str(env)])
    assert first.model == "from-file" and first.timeout == 12
    second = agent.parse_config(["--env-file", str(env), "--model", "from-cli"])
    assert second.model == "from-cli"


@pytest.mark.parametrize("platform", ["shopify", "woocommerce", "shopware", "jtl"])
def test_other_platform_is_not_cart_failure(platform):
    result = agent.new_result("shop.de")
    result.update(agent.classify_probe({"platforms": [platform]}))
    result["cart"] = agent.normalize_cart({})
    assert agent.determine_status(result) == "platform_mismatch"


def test_blocker_overrides_old_cart():
    result = agent.new_result("shop.de")
    result.update(agent.classify_probe({"platforms": ["magento"]}, ["__cf_bm"]))
    result.update(cart_verified=True, cart={"in_cart": True})
    assert agent.determine_status(result) == "blocked"
    assert result["cloudflare_markers"] == ["cookie:__cf_bm"]
    assert not agent.classify_probe({})["cloudflare"]


def test_success_self_report_does_not_replace_ground_truth():
    result = agent.new_result("shop.de")
    result.update(platform="magento", agent_success=True, cart=agent.normalize_cart({"found": True, "cart": {}}))
    assert agent.determine_status(result) == "cart_failed"


def test_read_error_is_not_empty_cart():
    result = agent.new_result("shop.de")
    result["cart"] = agent.normalize_cart({"reason": "cdp_read_error"})
    assert agent.determine_status(result) == "error"
    assert "cdp_read_error" in result["error"]


def test_guard_cancels_running_agent(config):
    stopped = []

    async def work(**kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            stopped.append(True)

    model = SimpleNamespace(run=work)
    observer = SimpleNamespace(wait_for_blocker=AsyncMock(return_value={"blocker": "Cloudflare"}))
    assert asyncio.run(agent.run_observed(model, config, observer)) is None
    assert stopped == [True]


@pytest.mark.parametrize("stop,expected", [(False, 3), (True, 2)])
def test_batch_continues_and_stop_uses_verified_cart(config, monkeypatch, stop, expected):
    config.stop_on_success = stop

    async def run_url(config, raw, index):
        result = agent.new_result(raw)
        result.update(status="screenshot_failed" if index == 2 else "error", cart_verified=index == 2)
        return result

    monkeypatch.setattr(agent, "run_url", run_url)
    asyncio.run(agent.run_batch(config, ["one", "two", "three"]))
    data = json.loads(open(config.output).read())
    assert len(data) == expected
    assert all(set(("url", "platform", "cloudflare", "status", "agent_success", "error", "final_result", "cart", "screenshot")) <= row.keys() for row in data)


def test_finalize_independent_read_and_screenshot_failure(config, monkeypatch):
    result = agent.new_result("https://shop.de")
    result.update(platform="magento", agent_success=True)
    reader = AsyncMock(return_value=agent.normalize_cart({"found": True, "cart": {"summary_count": 1}}))
    monkeypatch.setattr(agent, "read_cart", reader)
    monkeypatch.setattr(agent, "capture_checkout", AsyncMock(side_effect=PermissionError("denied")))
    observer = SimpleNamespace(inspect=AsyncMock(return_value={"platform": "magento"}))
    asyncio.run(agent.finalize_url(result, config, "http://localhost:1", 1, observer))
    reader.assert_awaited_once()
    assert result["cart_verified"] and result["status"] == "screenshot_failed"
    assert result["screenshot"] is None


def test_cart_only_is_not_full_success(config, monkeypatch):
    result = agent.new_result("https://shop.de")
    result["platform"] = "magento"
    monkeypatch.setattr(agent, "read_cart", AsyncMock(return_value=agent.normalize_cart({"found": True, "cart": {"summary_count": 1}})))
    monkeypatch.setattr(agent, "capture_checkout", AsyncMock(return_value=(None, None)))
    observer = SimpleNamespace(inspect=AsyncMock(return_value={}))
    asyncio.run(agent.finalize_url(result, config, "endpoint", 1, observer))
    assert result["status"] == "checkout_not_reached"


def test_secret_redaction(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sentinel-private-key")
    formatter = agent.SafeFormatter("%(message)s")
    record = logging.LogRecord("test", logging.ERROR, "", 1,
                               "sentinel-private-key http://user:password@proxy:8080", (), None)
    assert "sentinel-private-key" not in formatter.format(record)
    assert "password" not in formatter.format(record)


def test_preflight_block_does_not_call_agent_and_still_reads_cart(config, monkeypatch):
    page = SimpleNamespace(goto=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))

    @asynccontextmanager
    async def launch(_):
        yield context, object(), "endpoint"

    blocked = agent.classify_probe({"markers": ["challenge DOM"]})
    observer = SimpleNamespace(inspect=AsyncMock(return_value=blocked))
    monkeypatch.setattr(agent, "launch_browser", launch)
    monkeypatch.setattr(agent, "ShopObserver", lambda *args: observer)
    model = Mock(side_effect=AssertionError("Paid call forbidden"))
    monkeypatch.setattr(agent, "build_agent", model)
    reader = AsyncMock(return_value=agent.normalize_cart({}))
    monkeypatch.setattr(agent, "read_cart", reader)
    result = asyncio.run(agent.run_url(config, "https://shop.de", 1))
    model.assert_not_called()
    assert reader.await_count == 2
    assert result["status"] == "blocked" and result["agent_success"] is None
    assert agent.CURRENT_URL == "" and agent.CDP_URL == ""


@pytest.mark.parametrize("stage", ["launch", "init_script", "cookies"])
def test_partial_browser_startup_cleans_driver_and_temp_profile(config, tmp_path, monkeypatch, stage):
    pytest.importorskip("playwright")
    import playwright.async_api
    profile = tmp_path / ("magento-agent-" + stage)
    profile.mkdir()
    context = SimpleNamespace(close=AsyncMock(), add_init_script=AsyncMock(), add_cookies=AsyncMock())
    if stage == "init_script":
        context.add_init_script.side_effect = RuntimeError("script failure")
    config.__dict__.update(profile_dir=None, cdp_port=0, headless=True, channel=None,
                           executable_path=None, user_agent=None, proxy=None,
                           stealth_js=None, cookies=str(tmp_path / "missing.json") if stage == "cookies" else None)
    launch = AsyncMock(return_value=context)
    if stage == "launch":
        launch.side_effect = RuntimeError("launch failure")
    driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock())
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=driver)))
    monkeypatch.setattr(agent.tempfile, "mkdtemp", lambda **kwargs: str(profile))
    monkeypatch.setattr(agent.tempfile, "gettempdir", lambda: str(tmp_path))

    async def scenario():
        with pytest.raises((RuntimeError, OSError)):
            async with agent.launch_browser(config):
                pytest.fail("Failed startup must not yield")

    asyncio.run(scenario())
    driver.stop.assert_awaited_once()
    assert context.close.await_count == (0 if stage == "launch" else 1)
    assert not profile.exists()


def test_provider_string_and_no_cloud_fallback(monkeypatch):
    import sys
    model = Mock()
    monkeypatch.setitem(sys.modules, "browser_use", SimpleNamespace(Agent=model))
    monkeypatch.setattr(agent.importlib.metadata, "version", lambda name: "1.0.6")
    monkeypatch.setattr(agent, "make_tools", lambda bridge=None, capture=None: ("tools", None))
    config = SimpleNamespace(model="deepseek-flash", max_steps=5, timeout=10)
    agent.build_agent(config, "same-browser", "https://unknown.de")
    assert model.call_args.kwargs["llm"] == "deepseek:deepseek-flash"
    assert model.call_args.kwargs["browser"] == "same-browser"
    assert not model.call_args.kwargs["judge"] and not model.call_args.kwargs["web_search"]


def test_http_403_needs_cloudflare_server():
    context = SimpleNamespace(on=Mock())
    observer = agent.ShopObserver(context, "https://shop.de")
    request = SimpleNamespace(resource_type="document")
    for server in ("nginx", "cloudflare"):
        observer.on_response(SimpleNamespace(url="https://shop.de", status=403,
                                             request=request, headers={"server": server}))
    assert observer.response_markers == ["HTTP 403 Server: cloudflare"]


def test_current_page_uses_focus_not_old_tab_order():
    focused = SimpleNamespace(evaluate=AsyncMock(return_value=[True, 100]))
    stale = SimpleNamespace(evaluate=AsyncMock(return_value=[False, 200]))
    assert asyncio.run(agent.current_page([focused, stale])) is focused


@pytest.mark.parametrize("url,expected", [
    ("https://shop.de:8443/checkout?token=private#shipping", "https://shop.de:8443/checkout"),
    ("http://[::1]:8080/checkout", "http://[::1]:8080/checkout"),
])
def test_diagnostic_urls_preserve_port_and_ipv6(url, expected):
    assert agent.safe_url(url) == expected


def test_observer_detects_challenge_on_older_active_tab():
    def page(focused, markers):
        async def evaluate(script):
            if "document.hasFocus" in script:
                return [focused, 100]
            return {"platforms": [], "markers": markers, "blocker": None}
        return SimpleNamespace(url="https://shop.de/", is_closed=lambda: False, evaluate=evaluate)

    context = SimpleNamespace(pages=[page(True, ["challenge DOM"]), page(False, [])],
                              cookies=AsyncMock(return_value=[]), on=Mock())
    result = asyncio.run(agent.ShopObserver(context, "https://shop.de").inspect())
    assert result["cloudflare"] and result["blocker"] == "blocked by Cloudflare"


def test_cloudflare_response_remains_blocked_when_document_closes():
    context = SimpleNamespace(pages=[], on=Mock())
    observer = agent.ShopObserver(context, "https://shop.de")
    observer.on_response(SimpleNamespace(url="https://shop.de/challenge", status=403,
                                         request=SimpleNamespace(resource_type="document"),
                                         headers={"server": "cloudflare"}))
    result = asyncio.run(observer.inspect())
    assert result["cloudflare"] and result["blocker"] == "blocked by Cloudflare"
    assert "HTTP 403 Server: cloudflare" in result["cloudflare_markers"]


def test_old_login_tab_does_not_make_current_guest_checkout_mandatory_login():
    def page(focused, blocker):
        async def evaluate(script):
            if "document.hasFocus" in script:
                return [focused, 100]
            return {"platforms": ["magento"], "markers": [], "blocker": blocker}
        return SimpleNamespace(url="https://shop.de/checkout", is_closed=lambda: False, evaluate=evaluate)

    context = SimpleNamespace(pages=[page(True, None), page(False, "login wall")],
                              cookies=AsyncMock(return_value=[]), on=Mock())
    result = asyncio.run(agent.ShopObserver(context, "https://shop.de").inspect())
    assert result["blocker"] is None


def test_external_checkout_does_not_reclassify_original_shop(monkeypatch):
    context = SimpleNamespace(pages=[], cookies=AsyncMock(return_value=[]), on=Mock())
    observer = agent.ShopObserver(context, "https://shop.de")
    observer.latest = agent.classify_probe({"platforms": ["magento"]})
    external = SimpleNamespace(url="https://checkout.provider.com/checkout", is_closed=lambda: False,
                               evaluate=AsyncMock(return_value={"platforms": ["shopify"], "markers": []}))
    context.pages.append(external)
    monkeypatch.setattr(agent, "agent_target_page", AsyncMock(return_value=external))
    result = asyncio.run(observer.inspect())
    assert result["platform"] == "magento"


@pytest.mark.parametrize("phase", ["cart", "checkout"])
def test_stalled_document_finishes_with_diagnostic(phase, tmp_path, monkeypatch):
    # A page that never answers JS must not prevent all subsequent URLs from running.
    monkeypatch.setattr(agent, "PAGE_IO_TIMEOUT", .01, raising=False)
    monkeypatch.setattr(agent, "PAGE_PHASE_TIMEOUT", .1, raising=False)

    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()

    page = SimpleNamespace(url="https://shop.de/checkout", is_closed=lambda: False, evaluate=stalled, screenshot=stalled)

    @asynccontextmanager
    async def reader(_):
        yield SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])

    monkeypatch.setattr(agent, "cdp_reader", reader)

    async def scenario():
        operation = (agent.read_cart("endpoint", "https://shop.de") if phase == "cart" else
                     agent.capture_checkout("endpoint", "https://shop.de", tmp_path, 1))
        task = asyncio.create_task(operation)
        try:
            done, _ = await asyncio.wait([task], timeout=.5)
            assert task in done, "A stalled document exceeded the independent browser-read deadline"
            if phase == "cart":
                result = task.result()
                assert not result["in_cart"] and agent.cart_read_failed(result)
            else:
                with pytest.raises(TimeoutError):
                    task.result()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
