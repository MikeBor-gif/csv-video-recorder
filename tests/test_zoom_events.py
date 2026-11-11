import math

import pytest

from zoom_events import ZoomError, crop, parse_sidecar, ramp_frames, state_at
from zoom_events import load_sidecar


def metadata(events=None, **kwargs):
    return parse_sidecar(dict(viewport={"width": 1280, "height": 720}, fps=25,
                             zoom_factor=2, ramp_sec=.5, events=events or [], **kwargs))


def event(t, kind="in", x=640, y=360):
    return dict(t=t, type=kind, x=x, y=y)


def test_frames_and_easing():
    m = metadata([event(1), event(4, "out")])
    assert ramp_frames(.5, 25) == 13
    assert state_at(m, 1)[0] == 1
    assert state_at(m, 1.26)[0] == pytest.approx(1.5)
    assert state_at(m, 2)[0] == 2
    assert state_at(m, 4.26)[0] == pytest.approx(1.5)
    assert state_at(m, 5)[0] == 1


def test_multiple_and_open_ended():
    m = metadata([event(1), event(3, "out"), event(5, x=10)])
    assert state_at(m, 4)[0] == 1
    assert state_at(m, 100) == (2, 10, 360)


def test_interrupted_ramps_are_continuous():
    m = metadata([event(1), event(1.1, "out"), event(1.2, x=50)])
    for t in (1.1, 1.2):
        assert state_at(m, t - 1e-8) == pytest.approx(state_at(m, t), abs=1e-5)


@pytest.mark.parametrize("point,expected", [((0, 0), (0, 0)), ((1280, 720), (640, 360)), ((640, 360), (320, 180))])
def test_crop(point, expected):
    assert crop(1280, 720, 2, *point) == (*expected, 640, 360)


def test_legacy_and_boundary():
    m = metadata([dict(t=1, x=1280, y=720), dict(t=2, x=1, y=1)])
    assert [e["type"] for e in m["events"]] == ["in", "out"]
    assert m["events"][0]["x"] == 1279


@pytest.mark.parametrize("events", [[event(-1)], [event(math.nan)], [event(1, "out")],
    [event(2), event(1, "out")], [event(1), event(2)], [event(1, x=1281)],
    [event(1), dict(t=2, x=1, y=1)], [dict(t=0, x=True, y=1)]])
def test_bad_events(events):
    with pytest.raises(ZoomError):
        metadata(events)


@pytest.mark.parametrize("key,value", [("fps", 0), ("fps", True), ("ramp_sec", -1), ("zoom_factor", .5), ("fps", math.inf)])
def test_bad_settings(key, value):
    m = metadata()
    m[key] = value
    with pytest.raises(ZoomError):
        parse_sidecar(m)


@pytest.mark.parametrize("raw", ["{", "null", "[]", '{"viewport":false}'])
def test_bad_json(raw):
    with pytest.raises(ZoomError):
        parse_sidecar(raw)


def test_overflowing_numbers_are_validation_errors():
    m = metadata()
    m["fps"] = 10 ** 1000
    with pytest.raises(ZoomError):
        parse_sidecar(m)
    m["fps"], m["ramp_sec"] = 1e300, 1e300
    with pytest.raises(ZoomError):
        parse_sidecar(m)


def test_integer_ramp_product_overflow_is_validation_error():
    m = metadata()
    m["fps"], m["ramp_sec"] = 10**200, 10**200
    with pytest.raises(ZoomError):
        parse_sidecar(m)


def test_invalid_sidecar_encoding_is_validation_error(tmp_path):
    sidecar = tmp_path / "invalid.json"
    sidecar.write_bytes(b"\xff")
    with pytest.raises(ZoomError):
        load_sidecar(sidecar)
