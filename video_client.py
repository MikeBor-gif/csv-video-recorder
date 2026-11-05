"""Client functions used by the recorder to talk to web_service.py.

Public API:
    UploadVideo(server_url, video_path, company_name) -> str   uploads a video, returns its URL
    GetVideoURL(server_url, company_name) -> str               looks up the URL of a company
    PreviewVideo(video_url, view_seconds=10)                   opens the URL in Chromium
    VideoClientError                                           raised on any client failure

``requests`` and Playwright are imported lazily inside the functions, so importing
this module (or running the recorder without --server) does not require them.
Log verbosity follows QSCRIPT_LOG_LEVEL (DEBUG, INFO, WARN, ERROR; default INFO).
"""

import os

_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}
_LOG_THRESHOLD = _LOG_LEVELS.get(os.environ.get("QSCRIPT_LOG_LEVEL", "INFO").upper(), 20)

CONNECT_TIMEOUT_SECONDS = 5
UPLOAD_TIMEOUT_SECONDS = 300
LOOKUP_TIMEOUT_SECONDS = 30
DEFAULT_VIEW_SECONDS = 10


class VideoClientError(RuntimeError):
    """Upload, lookup or preview failed."""


def _log(company, message, level="INFO"):
    if _LOG_LEVELS.get(level, 20) < _LOG_THRESHOLD:
        return
    print(f"[{level}] [{company}] {message}", flush=True)


def _endpoint(server_url, path):
    return server_url.rstrip("/") + path


def _error_message(response):
    try:
        detail = response.json().get("error", "")
    except ValueError:
        detail = response.text[:200]
    return f"HTTP {response.status_code}: {detail}".rstrip(": ")


def _video_url_from(response, company_name):
    try:
        video_url = response.json()["video_url"]
    except (ValueError, KeyError, TypeError) as exc:
        raise VideoClientError(f"unexpected server response for {company_name!r}: {exc}") from exc
    if not video_url:
        raise VideoClientError(f"server returned an empty video_url for {company_name!r}")
    return video_url


def UploadVideo(server_url: str, video_path: str, company_name: str) -> str:
    """Uploads a video file to the server and returns its URL."""
    import requests  # lazy: the recorder must work without requests when --server is unused

    if not os.path.isfile(video_path):
        raise VideoClientError(f"video file not found: {video_path}")
    url = _endpoint(server_url, "/upload")
    _log(company_name, f"uploading {video_path} ({os.path.getsize(video_path)} bytes) -> {url}",
         "DEBUG")
    try:
        with open(video_path, "rb") as fileobj:
            response = requests.post(
                url,
                files={"file": (os.path.basename(video_path), fileobj)},
                data={"company_name": company_name},
                timeout=(CONNECT_TIMEOUT_SECONDS, UPLOAD_TIMEOUT_SECONDS),
            )
    except requests.RequestException as exc:
        raise VideoClientError(f"upload to {url} failed: {exc}") from exc
    if response.status_code != 200:
        raise VideoClientError(f"upload rejected: {_error_message(response)}")
    video_url = _video_url_from(response, company_name)
    _log(company_name, f"uploaded -> {video_url}", "DEBUG")
    return video_url


def GetVideoURL(server_url: str, company_name: str) -> str:
    """Returns the video URL for the given company name."""
    import requests  # lazy: see UploadVideo

    url = _endpoint(server_url, "/get_video_url")
    _log(company_name, f"looking up video URL at {url}", "DEBUG")
    try:
        response = requests.get(
            url,
            params={"company_name": company_name},
            timeout=(CONNECT_TIMEOUT_SECONDS, LOOKUP_TIMEOUT_SECONDS),
        )
    except requests.RequestException as exc:
        raise VideoClientError(f"lookup at {url} failed: {exc}") from exc
    if response.status_code == 404:
        raise VideoClientError(f"company not found: {company_name!r}")
    if response.status_code != 200:
        raise VideoClientError(f"lookup rejected: {_error_message(response)}")
    video_url = _video_url_from(response, company_name)
    _log(company_name, f"video URL -> {video_url}", "DEBUG")
    return video_url


def PreviewVideo(video_url: str, view_seconds: float = DEFAULT_VIEW_SECONDS,
                 company_name: str = "preview") -> None:
    """Opens ``video_url`` in a visible Chromium window (built-in media player).

    view_seconds > 0 keeps the window open that many seconds (or until the user
    closes it); view_seconds == 0 keeps it open until Enter is pressed.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    if view_seconds < 0:
        raise VideoClientError(f"view_seconds must be >= 0, got {view_seconds}")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            page = browser.new_page()
            try:
                page.goto(video_url)
            except PlaywrightError as exc:
                raise VideoClientError(f"preview of {video_url} failed: {exc}") from exc
            mode = f"{view_seconds:g}s" if view_seconds > 0 else "until Enter"
            _log(company_name, f"preview opened: {video_url} ({mode})")
            if view_seconds > 0:
                try:
                    # Returns early if the user closes the window.
                    page.wait_for_event("close", timeout=view_seconds * 1000)
                    _log(company_name, "preview window closed by the user", "DEBUG")
                except PlaywrightTimeoutError:
                    pass
            else:
                try:
                    input("Press Enter to close the preview...")
                except EOFError:
                    _log(company_name, "no interactive stdin; closing the preview", "WARN")
        finally:
            try:
                browser.close()
            except PlaywrightError:
                pass  # the user already closed the browser
    _log(company_name, "preview closed", "DEBUG")
