"""Browser automation scenario recorded to video (Playwright, sync API, Chromium).

Public API:
    PATH_TO_VIDEO_FOLDER  -- folder for recorded videos (may be overridden by the caller).
    CaptureVideo_Magento  -- runs the sample DuckDuckGo scenario and returns the .webm path.
    ShowCallout           -- shows a text callout over the page.

Log verbosity is controlled by the QSCRIPT_LOG_LEVEL environment variable
(DEBUG, INFO, WARN, ERROR; default INFO).
"""

import os
import random
import re
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Public configuration
# ---------------------------------------------------------------------------

PATH_TO_VIDEO_FOLDER = "./video"

VIDEO_SIZE = {"width": 1280, "height": 720}
# DuckDuckGo serves headless browsers less reliably; the video is recorded either way.
HEADLESS = False

SEARCH_BOX = 'textarea[name="q"]'
SEARCH_BUTTON = 'form button[type="submit"]'
FIRST_RESULT = 'a[data-testid="result-title-a"]'

NAV_TIMEOUT_MS = 30000
RESULTS_TIMEOUT_MS = 30000
TARGET_PAGE_TIMEOUT_MS = 15000

TYPE_DELAY_RANGE = (0.06, 0.20)
# Long enough for the click ripple to be visible before a navigation starts.
CLICK_PRESS_MS = 350
SPACE_EXTRA_DELAY_RANGE = (0.05, 0.15)

_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_LOG_THRESHOLD = _LOG_LEVELS.get(os.environ.get("QSCRIPT_LOG_LEVEL", "INFO").upper(), 20)

_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)
}

# The page of the running scenario; lets ShowCallout keep the 4-argument signature
# required by the task. Set and cleared only by CaptureVideo_Magento.
_active_page = None

# ---------------------------------------------------------------------------
# JavaScript resources
# ---------------------------------------------------------------------------

# Injected via context.add_init_script, so it runs in every new document
# (including the results page and the clicked site). Must be an IIFE.
OVERLAY_JS = r"""
(() => {
  const Z = 2147483647;
  const CURSOR_ID = '__qs_cursor';
  const HIGHLIGHT_ID = '__qs_highlight';

  const readPos = () => {
    try {
      const saved = JSON.parse(sessionStorage.getItem('__qs_pos') || 'null');
      if (saved && typeof saved.x === 'number') return saved;
    } catch (e) {}
    return {x: 0, y: 0};
  };
  const savePos = (pos) => {
    try { sessionStorage.setItem('__qs_pos', JSON.stringify(pos)); } catch (e) {}
  };

  const install = () => {
    if (document.getElementById(CURSOR_ID) || !document.documentElement) return;

    const style = document.createElement('style');
    style.textContent = `
      @keyframes __qs_ripple {
        from { transform: translate(-50%, -50%) scale(0.2); opacity: 0.95; }
        to   { transform: translate(-50%, -50%) scale(1); opacity: 0; }
      }`;

    const highlight = document.createElement('div');
    highlight.id = HIGHLIGHT_ID;
    highlight.style.cssText = `position: fixed; left: 0; top: 0; width: 64px; height: 64px;
      border-radius: 50%; background: rgba(255, 230, 0, 0.45);
      transform: translate(-50%, -50%); pointer-events: none; z-index: ${Z - 1};`;

    const cursor = document.createElement('div');
    cursor.id = CURSOR_ID;
    cursor.style.cssText = `position: fixed; left: 0; top: 0; width: 32px; height: 32px;
      pointer-events: none; z-index: ${Z};`;
    cursor.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"
      viewBox="0 0 32 32"><path d="M2 2 L2 27 L8.5 20.5 L13 30 L17.5 28 L13 18.5 L22.5 18.5 Z"
      fill="#ffffff" stroke="#000000" stroke-width="2" stroke-linejoin="round"/></svg>`;

    document.documentElement.append(style, highlight, cursor);

    const place = (x, y) => {
      for (const el of [highlight, cursor]) {
        el.style.left = x + 'px';
        el.style.top = y + 'px';
      }
    };
    const start = readPos();
    place(start.x, start.y);

    window.addEventListener('mousemove', (e) => {
      place(e.clientX, e.clientY);
      savePos({x: e.clientX, y: e.clientY});
    }, true);

    window.addEventListener('mousedown', (e) => {
      const ripple = document.createElement('div');
      ripple.style.cssText = `position: fixed; left: ${e.clientX}px; top: ${e.clientY}px;
        width: 72px; height: 72px; border-radius: 50%; box-sizing: border-box;
        border: 4px solid rgba(255, 110, 0, 0.95); background: rgba(255, 200, 0, 0.2);
        pointer-events: none; z-index: ${Z - 1};
        animation: __qs_ripple 0.6s ease-out forwards;`;
      ripple.addEventListener('animationend', () => ripple.remove());
      document.documentElement.append(ripple);
    }, true);
  };

  install();
  document.addEventListener('DOMContentLoaded', install);
})();
"""

CALLOUT_JS = r"""
async ({text, bgColor, textColor, fade, holdMs}) => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const box = document.createElement('div');
  box.textContent = text;
  box.style.cssText = `position: fixed; top: 90px; left: 50%; transform: translateX(-50%);
    display: inline-block; width: max-content; max-width: 90vw; padding: 10px 18px;
    background: ${bgColor}; color: ${textColor}; font: 16pt Arial, sans-serif;
    border-radius: 4px; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
    pointer-events: none; z-index: 2147483645;
    opacity: ${fade ? 0 : 1}; transition: opacity 2s ease;`;
  document.documentElement.append(box);
  if (fade) {
    await wait(50);
    box.style.opacity = '1';
    await wait(2000);
  }
  await wait(holdMs);
  if (fade) {
    box.style.opacity = '0';
    await wait(2000);
  }
  box.remove();
}
"""

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _log(company, message, level="INFO"):
    if _LOG_LEVELS.get(level, 20) < _LOG_THRESHOLD:
        return
    print(f"[{level}] [{company}] {message}", flush=True)


def _safe_filename(name):
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    if not cleaned:
        return "video"
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        return f"_{cleaned}"
    return cleaned


def _is_navigation_error(exc):
    text = str(exc).lower()
    return "destroyed" in text or "navigation" in text or "navigating" in text


class _Cursor:
    """Tracks the mouse position (Playwright does not expose it) and moves it smoothly."""

    def __init__(self, page, company):
        self.page = page
        self.company = company
        self.x = 0.0
        self.y = 0.0

    def move_to(self, x, y):
        distance = ((x - self.x) ** 2 + (y - self.y) ** 2) ** 0.5
        steps = max(15, int(distance / 10))
        _log(self.company, f"cursor ({self.x:.0f},{self.y:.0f}) -> ({x:.0f},{y:.0f}), steps={steps}", "DEBUG")
        self.page.mouse.move(x, y, steps=steps)
        self.x, self.y = x, y
        time.sleep(random.uniform(0.1, 0.25))

    def click_locator(self, locator, name, x_ratio=0.5):
        _log(self.company, f"moving cursor to {name}")
        locator.wait_for(state="visible")
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        if box is None:
            raise RuntimeError(f"{name} has no bounding box")
        x = box["x"] + box["width"] * x_ratio
        y = box["y"] + box["height"] / 2
        self.move_to(x, y)
        time.sleep(random.uniform(0.2, 0.4))
        # A short press keeps the ripple visible before a possible navigation.
        self.page.mouse.click(x, y, delay=CLICK_PRESS_MS)
        _log(self.company, f"clicked {name} at ({x:.0f},{y:.0f})", "DEBUG")

    def resync(self):
        """Re-emit mousemove so the overlay of a freshly loaded document sits under the cursor."""
        self.page.mouse.move(self.x + 1, self.y)
        self.page.mouse.move(self.x, self.y)


def _type_humanlike(page, company, text):
    _log(company, f'typing query: "{text}"')
    for ch in text:
        page.keyboard.type(ch)
        delay = random.uniform(*TYPE_DELAY_RANGE)
        if ch == " ":
            delay += random.uniform(*SPACE_EXTRA_DELAY_RANGE)
        time.sleep(delay)


def _open_page(page, company, url):
    page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    page.wait_for_selector(SEARCH_BOX, state="visible", timeout=NAV_TIMEOUT_MS)
    _log(company, f"opened {url}")


def _submit_search(page, cursor):
    # The submit button inside the search field is collapsed (zero width);
    # after typing, a visible "Search" button appears in the suggestions dropdown.
    button = page.locator(SEARCH_BUTTON).filter(visible=True).first
    try:
        cursor.click_locator(button, "search button")
    except PlaywrightError as exc:
        if not _is_navigation_error(exc):
            raise
        _log(cursor.company, f"expected navigation error ignored: {exc}", "DEBUG")
    page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    _log(cursor.company, "search submitted")


def _wait_results(page, cursor):
    started = time.monotonic()
    try:
        page.wait_for_selector(FIRST_RESULT, state="visible", timeout=RESULTS_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _log(cursor.company, "results did not appear, reloading the results page", "WARN")
        page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_selector(FIRST_RESULT, state="visible", timeout=RESULTS_TIMEOUT_MS)
    cursor.resync()
    _log(cursor.company, f"results visible ({time.monotonic() - started:.1f} s)")


def _click_first_result(page, cursor):
    result = page.locator(FIRST_RESULT).first
    href = result.get_attribute("href")
    results_url = page.url
    # Aim at the left third: the anchor box is the title text itself.
    cursor.click_locator(result, "first result", x_ratio=1 / 3)
    _log(cursor.company, f"first result clicked: {href}")
    try:
        # wait_for_load_state alone would return for the still-current results page.
        page.wait_for_url(
            lambda current: current != results_url,
            wait_until="domcontentloaded",
            timeout=TARGET_PAGE_TIMEOUT_MS,
        )
        cursor.resync()
        _log(cursor.company, f"target page loaded: {page.url}", "DEBUG")
    except PlaywrightError as exc:
        _log(cursor.company, f"target page still loading: {exc}", "WARN")
    page.wait_for_timeout(2500)


def _finalize_video(video, video_dir, company_name, succeeded):
    """Move the recorded video to <video_dir>/<company_name>.webm; call after context.close()."""
    if video is None:
        return None
    if not succeeded:
        # A page that closes before producing frames has no video artifact:
        # Playwright raises "Page closed" / "did not produce any video frames".
        try:
            video.delete()
            _log(company_name or "unnamed", "[FIX] partial video of failed scenario deleted", "DEBUG")
        except PlaywrightError as exc:
            _log(company_name or "unnamed", f"[FIX] no video to delete: {exc}", "DEBUG")
        return None
    company = company_name or "unnamed"
    target = video_dir / f"{_safe_filename(company_name)}.webm"
    _log(company, f"[FIX] finalizing video -> {target}", "DEBUG")
    try:
        if target.exists():
            target.unlink()
        video.save_as(str(target))
    except OSError as exc:
        # E.g. the previous video is open in a player (Windows locks it) or the name is too long.
        _log(company, f"cannot write {target}: {type(exc).__name__}: {exc}", "ERROR")
        return None
    finally:
        # Always remove Playwright's raw recording (random hash name) from the video folder.
        try:
            video.delete()
        except PlaywrightError as exc:
            _log(company, f"[FIX] raw recording not deleted: {exc}", "WARN")
    _log(company, f"saved: {target}")
    return str(target)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def ShowCallout(text, bgColor="#FFFF00", textColor="#000000", fade=True, page=None):
    """Show a yellow (by default) text callout; fade-in/out takes about 2 s each."""
    target = page if page is not None else _active_page
    if target is None:
        _log("callout", "no active page, callout skipped", "WARN")
        return
    _log("callout", f'show: "{text}" fade={fade}')
    try:
        target.evaluate(
            CALLOUT_JS,
            {"text": text, "bgColor": bgColor, "textColor": textColor, "fade": bool(fade), "holdMs": 1500},
        )
    except PlaywrightError as exc:
        _log("callout", f"callout interrupted: {exc}", "WARN")


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


def CaptureVideo_Magento(
    url,
    company_name,
    language,
    country,
    postcode,
    city,
    street,
    house_no,
):
    """Record the sample DuckDuckGo scenario; return the absolute .webm path or None."""
    global _active_page

    company = company_name or "unnamed"
    _log(company, f"start: url={url} city={city} language={language} country={country}")
    _log(company, f"address: {postcode} {street} {house_no}", "DEBUG")

    video = None
    succeeded = False
    try:
        video_dir = Path(PATH_TO_VIDEO_FOLDER).resolve()
        video_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=HEADLESS)
            context = browser.new_context(
                viewport=VIDEO_SIZE,
                record_video_dir=str(video_dir),
                record_video_size=VIDEO_SIZE,
            )
            try:
                context.add_init_script(script=OVERLAY_JS)
                page = context.new_page()
                video = page.video
                _active_page = page
                cursor = _Cursor(page, company)
                query = f"Magento developer in {city}"

                _open_page(page, company, url)
                cursor.move_to(VIDEO_SIZE["width"] / 2, VIDEO_SIZE["height"] / 3)
                ShowCallout(query, "#FFFF00", "#000000", True)
                cursor.click_locator(page.locator(SEARCH_BOX), "search box")
                _type_humanlike(page, company, query)
                _submit_search(page, cursor)
                _wait_results(page, cursor)
                _click_first_result(page, cursor)
                succeeded = True
            except Exception as exc:
                _log(company, f"scenario failed: {type(exc).__name__}: {exc}", "ERROR")
            finally:
                _active_page = None
                # Closing the context finalizes the video file.
                try:
                    context.close()
                except Exception as exc:
                    _log(company, f"context close failed: {exc}", "WARN")

            # The video must be saved while the Playwright driver is still running.
            try:
                return _finalize_video(video, video_dir, company_name, succeeded)
            finally:
                try:
                    browser.close()
                except Exception as exc:
                    _log(company, f"browser close failed: {exc}", "WARN")
    except Exception as exc:
        _log(company, f"failed: {type(exc).__name__}: {exc}", "ERROR")
        return None
