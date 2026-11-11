import json
from types import SimpleNamespace

import pytest

from qscript import _ZoomCapture


def collector():
    capture = _ZoomCapture(epoch=1000)
    capture.page = SimpleNamespace(main_frame=object())
    return capture, dict(page=capture.page, frame=capture.page.main_frame)


def test_toggle_state_and_trigger_timestamp_survive_documents():
    c, source = collector()
    first = c.binding(source, dict(action="toggle", epoch=3000, x=100, y=200))
    assert first == dict(t=2, x=100, y=200, type="in")
    assert c.binding(source, dict(action="state"))["type"] == "in"
    second = c.binding(source, dict(action="toggle", epoch=6000, x=300, y=400))
    assert second["type"] == "out"
    assert second["t"] == 5


def test_sidecar_nonempty_and_stale_cleanup(tmp_path):
    c, source = collector()
    video = tmp_path / "ACME.webm"
    video.write_bytes(b"video")
    c.binding(source, dict(epoch=3000, x=10, y=20))
    c.save(str(video))
    sidecar = tmp_path / "ACME.webm.zoom.json"
    assert json.loads(sidecar.read_text())["events"][0]["type"] == "in"
    assert c.save(None) is None
    assert sidecar.exists()
    plain, _ = collector()
    plain.save(str(video))
    assert not sidecar.exists()


def test_bad_trigger_does_not_change_toggle():
    c, source = collector()
    with pytest.raises(ValueError):
        c.binding(source, dict(epoch=2000, x=-1, y=0))
    assert not c.events


def test_subframe_rejected():
    c, source = collector()
    source["frame"] = object()
    with pytest.raises(ValueError, match="top-level"):
        c.binding(source, dict(action="state"))


def test_csv_continues_after_processing_failure(tmp_path, monkeypatch):
    import capture_video
    import qscript
    import video_client
    csv = tmp_path / "input.csv"
    csv.write_text("URL;Company Name;Language;Country;Postcode;City;Street;HouseNo\n"
                   "http://test;First;en;GB;1;City;Street;1\n"
                   "http://test;Second;en;GB;1;City;Street;1\n", encoding="utf-8")
    seen = []
    monkeypatch.setattr(qscript, "CaptureVideo_Magento", lambda **kwargs: "/tmp/video.webm")
    def upload(server, path, company):
        seen.append(company)
        if company == "First":
            raise video_client.VideoClientError("job failed")
        return "http://server/media/zoomed.webm"
    monkeypatch.setattr(video_client, "UploadVideo", upload)
    monkeypatch.setattr(video_client, "PreviewVideo", lambda *args, **kwargs: None)
    monkeypatch.setattr(qscript, "ZOOM_DEMO", False)
    assert capture_video.main([str(csv), "--server", "http://server", "--zoom-demo"]) == 0
    assert seen == ["First", "Second"]


def test_failed_save_preserves_previous_recording(tmp_path):
    import qscript
    from pathlib import Path
    previous = tmp_path / "ACME.webm"
    previous.write_bytes(b"previous-good-video")

    class BrokenVideo:
        def save_as(self, path):
            Path(path).write_bytes(b"partial-new-video")
            raise OSError("simulated save failure")

        def delete(self):
            pass

    assert qscript._finalize_video(BrokenVideo(), tmp_path, "ACME", True) is None
    assert previous.read_bytes() == b"previous-good-video"
    assert list(tmp_path.iterdir()) == [previous]
