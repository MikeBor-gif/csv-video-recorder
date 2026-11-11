import json
import subprocess
from unittest.mock import patch

import pytest

from apply_zoom import build_filter, calibrate_metadata, display_command, main, process_video
from tests.test_zoom_events import event, metadata
from zoom_events import ZoomError


def input_files(tmp_path, events=None):
    video = tmp_path / "a file.webm"
    video.write_bytes(b"original")
    sidecar = tmp_path / "a file.webm.zoom.json"
    sidecar.write_text(json.dumps(metadata(events)), encoding="utf-8")
    return video


def test_no_events_copies_bytes(tmp_path):
    video = input_files(tmp_path)
    with patch("apply_zoom.subprocess.run") as run:
        output = process_video(video)
    assert open(output, "rb").read() == b"original"
    run.assert_not_called()


def test_no_sidecar_copies(tmp_path):
    video = tmp_path / "plain.webm"
    video.write_bytes(b"plain")
    assert open(process_video(video), "rb").read() == b"plain"


def test_dry_run_no_binary_no_writes(tmp_path, capsys):
    video = input_files(tmp_path, [event(0)])
    before = set(tmp_path.iterdir())
    with patch("apply_zoom.subprocess.run") as run:
        assert main(["--video", str(video), "--dry-run", "--ffmpeg", "missing binary"]) == 0
    assert set(tmp_path.iterdir()) == before
    assert "zoompan" in capsys.readouterr().out
    run.assert_not_called()


def test_filter_absolute_timeline():
    result = build_filter(metadata([event(1), event(4, "out"), event(5)]))
    assert "fps=25,zoompan=" in result
    assert "d=1:s=1280x720:fps=25" in result
    assert "on/25" in result
    assert "iw-iw/zoom" in result


def test_timeout_cleanup_and_preserve_output(tmp_path):
    video = input_files(tmp_path, [event(0)])
    output = tmp_path / "output.webm"
    output.write_bytes(b"old")
    with patch("apply_zoom.probe", return_value=dict(width=1280, height=720, duration=2)), \
         patch("apply_zoom.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
        with pytest.raises(ZoomError, match="timed out"):
            process_video(video, output=output, timeout=1)
    assert output.read_bytes() == b"old"
    assert not list(tmp_path.glob(".*.part.webm"))


def test_reject_same_input(tmp_path):
    video = input_files(tmp_path)
    with pytest.raises(ZoomError):
        process_video(video, output=video)


def test_dimensions_must_match(tmp_path):
    video = input_files(tmp_path, [event(0)])
    with patch("apply_zoom.probe", return_value=dict(width=100, height=100, duration=2)):
        with pytest.raises(ZoomError, match="dimensions"):
            process_video(video)


def test_explicit_missing_sidecar_is_error(tmp_path):
    video = input_files(tmp_path)
    with pytest.raises(ZoomError, match="sidecar file not found"):
        process_video(video, sidecar=tmp_path / "missing.json")


def test_success_publishes_atomically(tmp_path):
    video = input_files(tmp_path, [event(0)])
    def encode(command, **kwargs):
        from pathlib import Path
        Path(command[-1]).write_bytes(b"encoded")
        return subprocess.CompletedProcess(command, 0, "", "")
    with patch("apply_zoom.probe", return_value=dict(width=1280, height=720, duration=2)), \
         patch("apply_zoom.subprocess.run", side_effect=encode):
        output = process_video(video)
    assert open(output, "rb").read() == b"encoded"
    assert video.read_bytes() == b"original"
    assert not list(tmp_path.glob(".*.part.webm"))


def test_nonzero_ffmpeg_does_not_publish(tmp_path):
    video = input_files(tmp_path, [event(0)])
    with patch("apply_zoom.probe", return_value=dict(width=1280, height=720, duration=2)), \
         patch("apply_zoom.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "bad encoder")):
        with pytest.raises(ZoomError, match="encoding failed"):
            process_video(video)
    assert not (tmp_path / "a file.zoomed.webm").exists()


def test_event_after_duration_rejected(tmp_path):
    video = input_files(tmp_path, [event(3)])
    with patch("apply_zoom.probe", return_value=dict(width=1280, height=720, duration=2)):
        with pytest.raises(ZoomError, match="beyond end"):
            process_video(video)


def test_dry_run_copy(tmp_path, capsys):
    video = input_files(tmp_path)
    process_video(video, dry_run=True)
    assert "copy " in capsys.readouterr().out
    assert not (tmp_path / "a file.zoomed.webm").exists()


def test_marker_calibration_corrects_clock():
    m = metadata([event(2)])
    m["calibration"] = dict(t=1, marker="magenta-square-v1")
    pixels = b'\xff\xff\xff' * 5 + b'\xff\x00\xff'
    with patch("apply_zoom.subprocess.run", return_value=subprocess.CompletedProcess([], 0, pixels, b"")):
        corrected = calibrate_metadata("input.webm", m, "ffmpeg")
    assert corrected["events"][0]["t"] == pytest.approx(1.2)
    assert m["events"][0]["t"] == 2


def test_many_zoom_pairs_fit_windows_process_limit(tmp_path):
    from pathlib import Path
    video = input_files(tmp_path, [event(i*.1, "in" if i % 2 == 0 else "out",
                                       x=100+(i*9) % 1000, y=100+(i*5) % 500) for i in range(96)])

    def encode(command, **kwargs):
        # Model the actual Windows CreateProcess limit, independently of filter transport.
        if len(display_command(command)) >= 32767:
            raise OSError("[WinError 206] command line is too long")
        Path(command[-1]).write_bytes(b"encoded")
        return subprocess.CompletedProcess(command, 0, "", "")

    with patch("apply_zoom.probe", return_value=dict(width=1280, height=720, duration=20)), \
         patch("apply_zoom.subprocess.run", side_effect=encode):
        output = process_video(video)
    assert Path(output).read_bytes() == b"encoded"
    assert not list(tmp_path.glob(".*")), "processing must clean up temporary filter files"
