"""Real Chromium tests; no external site is needed for F5/navigation checks."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest
from playwright.sync_api import Error, sync_playwright

import qscript
from apply_zoom import calibrate_metadata, process_video
from zoom_events import load_sidecar

pytestmark = pytest.mark.browser


@pytest.fixture
def local_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/outer-frame"):
                content = f'<iframe style="position:absolute;left:40px;top:30px;width:500px;height:350px;border:3px solid" src="http://127.0.0.1:{self.server.server_port}/inner-frame"></iframe>'
            elif self.path.startswith("/inner-frame"):
                content = '<input id="field" style="position:absolute;left:20px;top:20px">'
            elif self.path.startswith("/results"):
                content = f'<a data-testid="result-title-a" href="http://localhost:{self.server.server_port}/target">Result</a>'
            else:
                content = '<h1>Zoom test</h1><form action="/results"><textarea name="q"></textarea><button type="submit">Search</button></form>'
            body = ("<html><body style='margin:0;background:#ffffff'>"+content+"</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
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


@pytest.fixture
def browser():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Chromium missing; python -m playwright install chromium")
            raise
        yield browser
        browser.close()


def install(browser, tmp_path):
    context = browser.new_context(viewport=qscript.VIDEO_SIZE, record_video_size=qscript.VIDEO_SIZE,
                                  record_video_dir=str(tmp_path))
    capture = qscript._ZoomCapture()
    context.expose_binding("__qsZoomEvent", capture.binding)
    context.add_init_script(script=qscript.ZOOM_JS)
    page = context.new_page()
    capture.page = page
    capture.epoch = time.time() * 1000
    qscript._calibrate_zoom(page)
    return context, page, capture


def test_f5_hook_navigation_and_repeat(browser, local_site, tmp_path):
    context, page, capture = install(browser, tmp_path)
    try:
        page.goto(f"http://127.0.0.1:{local_site}/first")
        page.evaluate("() => window.__zoomReady")
        page.evaluate("window.navigationSentinel = 42")
        page.mouse.move(123, 234)
        page.keyboard.down("F5")
        page.keyboard.down("F5")  # autorepeat must not toggle a second time
        page.keyboard.up("F5")
        page.evaluate("() => window.__zoomFlush()")
        assert page.evaluate("window.navigationSentinel") == 42
        assert capture.events[0]["type"] == "in"
        assert (capture.events[0]["x"], capture.events[0]["y"]) == (123, 234)
        assert page.locator('[data-zoom-indicator="in"]').count() == 1
        page.goto(f"http://localhost:{local_site}/other-origin")
        page.evaluate("() => window.__zoomReady")
        out = page.evaluate("() => window.__zoomAt(200,300)")
        assert out["type"] == "out"
        assert out["t"] >= capture.events[0]["t"]
        page.keyboard.press("F5")
        page.evaluate("() => window.__zoomFlush()")
        assert [e["type"] for e in capture.events] == ["in", "out", "in"]
        assert (capture.events[-1]["x"], capture.events[-1]["y"]) == (123, 234)
    finally:
        context.close()


@pytest.mark.parametrize("scale", [1, .75])
def test_nested_cross_origin_frame_f5_and_hook(browser, local_site, tmp_path, scale):
    context, page, capture = install(browser, tmp_path)
    try:
        page.goto(f"http://127.0.0.1:{local_site}/first")
        page.evaluate(f'''() => {{
          document.body.innerHTML = '<iframe style="position:absolute;left:200px;top:100px;width:600px;height:500px;border:2px solid;transform-origin:0 0;transform:scale({scale})" src="http://localhost:{local_site}/outer-frame"></iframe>';
        }}''')
        field = page.frame_locator("iframe").frame_locator("iframe").locator("#field")
        field.click(position={"x": 10, "y": 10})
        box = field.bounding_box()
        page.mouse.move(box["x"] + 10, box["y"] + 10)
        frame = page.frames[-1]
        frame.evaluate("window.sentinel = 42")
        page.keyboard.down("F5")
        page.keyboard.down("F5")
        page.keyboard.up("F5")
        frame.evaluate("() => window.__zoomFlush()")
        assert len(capture.events) == 1
        # Chromium rounds mouse client coordinates in scaled child documents.
        assert capture.events[0]["x"] == pytest.approx(box["x"] + 10, abs=1)
        assert capture.events[0]["y"] == pytest.approx(box["y"] + 10, abs=1)
        assert frame.evaluate("window.sentinel") == 42
        assert page.locator('[data-zoom-indicator="in"]').count() == 1
        accepted = frame.evaluate("() => window.__zoomAt(50,60)")
        assert accepted["type"] == "out"
        assert (accepted["x"], accepted["y"]) == (200 + 95*scale, 100 + 95*scale)
        with pytest.raises(Error, match="invalid mouse coordinate"):
            frame.evaluate("() => window.__zoomAt(NaN,60)")
        # Focus stays in the frame while the cursor returns to the main document.
        page.mouse.move(20, 30)
        page.keyboard.press("F5")
        frame.evaluate("() => window.__zoomFlush()")
        assert (capture.events[-1]["x"], capture.events[-1]["y"]) == (20, 30)
    finally:
        context.close()


@pytest.mark.encoding
def test_recorded_marker_and_open_zoom(browser, local_site, tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("local ffmpeg/ffprobe are required for video calibration")
    context, page, capture = install(browser, tmp_path)
    video = page.video
    try:
        page.goto(f"http://127.0.0.1:{local_site}/marker")
        page.evaluate("() => window.__zoomReady")
        # A visible marker in the same JS turn as the event, independent of Python delivery.
        page.wait_for_timeout(500)
        accepted = page.evaluate("() => { document.body.style.background='#ff0000'; return window.__zoomAt(640,360); }")
        page.wait_for_timeout(900)
    finally:
        context.close()
    saved = tmp_path / "marker.webm"
    video.save_as(str(saved))
    video.delete()
    capture.save(str(saved))
    frames = subprocess.run(["ffmpeg", "-v", "error", "-i", str(saved), "-vf",
                             "crop=2:2:10:10,scale=1:1,format=rgb24", "-f", "rawvideo", "-"],
                            capture_output=True, check=True, timeout=30).stdout
    pixels = [frames[i:i+3] for i in range(0, len(frames), 3)]
    first_red = next(i for i, rgb in enumerate(pixels) if rgb[0] > 160 and rgb[1] < 60 and rgb[2] < 60)
    corrected = calibrate_metadata(saved, load_sidecar(str(saved)+".zoom.json"), "ffmpeg")
    error = corrected["events"][0]["t"] - first_red / 25
    print(f"marker event/frame offset={error:.4f}s (25fps)")
    assert abs(error) <= 1 / 25 + .005
    assert load_sidecar(str(saved) + ".zoom.json")["events"][-1]["type"] == "in"
    output = process_video(saved)
    assert Path(output).stat().st_size > 0


def test_full_recorder_demo_pair(local_site, tmp_path, monkeypatch):
    # The recorder owns its Playwright loop: finish this preflight before calling it.
    with sync_playwright() as p:
        if not Path(p.chromium.executable_path).is_file():
            pytest.skip("Chromium missing; python -m playwright install chromium")
    monkeypatch.setattr(qscript, "HEADLESS", True)
    monkeypatch.setattr(qscript, "PATH_TO_VIDEO_FOLDER", str(tmp_path))
    monkeypatch.setattr(qscript, "ZOOM_DEMO", True)
    mouse_points = []
    original_type = qscript._type_humanlike

    def type_at_search_field(page, company, text):
        # Derive the expected point from actual layout, independently of zoom code.
        box = page.locator(qscript.SEARCH_BOX).bounding_box()
        mouse_points.append((box["x"] + box["width"] / 2, box["y"] + box["height"] / 2))
        original_type(page, company, text)

    monkeypatch.setattr(qscript, "_type_humanlike", type_at_search_field)
    result = qscript.CaptureVideo_Magento(f"http://127.0.0.1:{local_site}/", "Local Zoom Co",
                                         "en", "GB", "1", "Test", "Street", "1")
    assert result is not None
    assert Path(result).stat().st_size > 1000
    sidecar = load_sidecar(str(result)+".zoom.json")
    assert [e["type"] for e in sidecar["events"]] == ["in", "out"]
    assert mouse_points[0] != (640, 360), "fixture must expose a screen-center targeting bug"
    for event in sidecar["events"]:
        assert (event["x"], event["y"]) == pytest.approx(mouse_points[0], abs=1)
    assert sidecar["calibration"]["marker"] == "magenta-square-v1"


def test_f5_preserves_mouse_move_during_bootstrap(browser):
    context = browser.new_context(viewport=qscript.VIDEO_SIZE)
    capture = qscript._ZoomCapture()
    context.expose_binding("__qsZoomEvent", capture.binding)
    delayed_state = """(() => {
      const original = window.__qsZoomEvent;
      window.__qsZoomEvent = payload => original(payload).then(value =>
        payload.action === 'state'
          ? new Promise(resolve => setTimeout(() => resolve(value), 500)) : value);
    })();"""
    context.add_init_script(script=delayed_state + "\n" + qscript.ZOOM_JS)
    try:
        page = context.new_page()
        capture.page = page
        page.mouse.move(123, 234)
        page.evaluate("() => window.__zoomReady")
        page.keyboard.press("F5")
        page.evaluate("() => window.__zoomFlush()")
        assert (capture.events[0]["x"], capture.events[0]["y"]) == (123, 234)
    finally:
        context.close()
