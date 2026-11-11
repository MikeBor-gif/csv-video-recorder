"""Pure, dependency-free sidecar validation and eased zoom timeline."""

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("apply_zoom.sidecar")


class ZoomError(ValueError):
    """Invalid zoom metadata or failed processing."""


def _number(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ZoomError(f"{label} must be a number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < minimum:
        raise ZoomError(f"{label} must be finite and >= {minimum}")
    return value


def parse_sidecar(data):
    """Validate and normalize a dict/JSON string; never silently reorder events."""
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ZoomError(f"invalid sidecar JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ZoomError("sidecar must be an object")
    viewport = data.get("viewport")
    if not isinstance(viewport, dict):
        raise ZoomError("viewport is required")
    size = {}
    for key in ("width", "height"):
        value = viewport.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ZoomError(f"viewport.{key} must be a positive integer")
        _number(value, f"viewport.{key}", 1)
        size[key] = value
    fps = _number(data.get("fps"), "fps")
    ramp = _number(data.get("ramp_sec"), "ramp_sec")
    factor = _number(data.get("zoom_factor"), "zoom_factor", 1)
    if not fps or not ramp:
        raise ZoomError("fps and ramp_sec must be positive")
    _number(fps * ramp, "ramp frame count")
    events = data.get("events")
    if not isinstance(events, list):
        raise ZoomError("events must be a list")
    if any(not isinstance(e, dict) for e in events):
        raise ZoomError("each event must be an object")
    typed = ["type" in e for e in events]
    if any(typed) and not all(typed):
        raise ZoomError("cannot mix typed and legacy events")
    normalized = []
    previous = 0
    for i, event in enumerate(events):
        t = _number(event.get("t"), "event.t")
        if t < previous:
            raise ZoomError("events must be in chronological order")
        expected = "in" if i % 2 == 0 else "out"
        kind = event.get("type", expected)
        if kind != expected:
            raise ZoomError(f"expected {expected!r} at event {i}")
        point = {}
        for axis, dimension in (("x", "width"), ("y", "height")):
            v = _number(event.get(axis), f"event.{axis}")
            if v > size[dimension]:
                raise ZoomError(f"event.{axis} is outside viewport")
            point[axis] = min(v, size[dimension] - 1)
        normalized.append(dict(t=t, type=kind, **point))
        previous = t
    result = dict(viewport=size, fps=fps, zoom_factor=factor,
                  ramp_sec=ramp, events=normalized)
    if "calibration" in data:
        calibration = data["calibration"]
        if not isinstance(calibration, dict) or calibration.get("marker") != "magenta-square-v1":
            raise ZoomError("unknown video calibration marker")
        result["calibration"] = dict(t=_number(calibration.get("t"), "calibration.t"),
                                    marker="magenta-square-v1")
    return result


def load_sidecar(path):
    logger.debug("[FIX:sidecar] reading path=%s", path)
    try:
        data = parse_sidecar(Path(path).read_bytes())
    except ZoomError as exc:
        logger.warning("[FIX:sidecar] rejected path=%s: %s", path, exc)
        raise
    logger.debug("[FIX:sidecar] parsed path=%s events=%d", path, len(data["events"]))
    return data


def ramp_frames(ramp_sec, fps):
    return max(1, math.floor(ramp_sec * fps + 0.5))


def ease(u):
    u = max(0.0, min(1.0, u))
    return u * u * (3 - 2 * u)


def crop(width, height, zoom, x, y):
    """Crop stays inside the input frame, including at frame edges."""
    cw, ch = width / zoom, height / zoom
    return max(0, min(x - cw / 2, width - cw)), max(0, min(y - ch / 2, height - ch)), cw, ch


@dataclass(frozen=True)
class Transition:
    t: float
    start: tuple
    end: tuple
    duration: float

    def at(self, t):
        u = ease((t - self.t) / self.duration)
        return tuple(a + (b - a) * u for a, b in zip(self.start, self.end))


def transitions(metadata):
    """Build compact segments; interrupted ramps start at their achieved value."""
    w, h = metadata["viewport"]["width"], metadata["viewport"]["height"]
    state = (1.0, w / 2, h / 2)
    result = []
    duration = ramp_frames(metadata["ramp_sec"], metadata["fps"]) / metadata["fps"]
    for event in metadata["events"]:
        if result:
            state = result[-1].at(event["t"])
        if event["type"] == "in":
            # At full frame the anchor is invisible, so initialize at the exact point.
            if abs(state[0] - 1) < 1e-9:
                state = (1.0, event["x"], event["y"])
            target = (metadata["zoom_factor"], event["x"], event["y"])
        else:
            target = (1.0, state[1], state[2])
        result.append(Transition(event["t"], state, target, duration))
    return result


def state_at(metadata, t):
    state = (1.0, metadata["viewport"]["width"] / 2, metadata["viewport"]["height"] / 2)
    for segment in transitions(metadata):
        if t < segment.t:
            break
        state = segment.at(t)
    return state
