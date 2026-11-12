"""Render recorded zoom events with ffmpeg zoompan; importable without side effects."""

import argparse
import json
import logging
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import uuid

from zoom_events import ZoomError, load_sidecar, transitions

logger = logging.getLogger("apply_zoom")


def _fmt(value):
    return format(value, ".12g")


def _expression(metadata, axis, initial):
    segments = transitions(metadata)
    if not segments:
        return _fmt(initial)
    clock = f"on/{_fmt(metadata['fps'])}"
    values = [_fmt(initial)]
    for segment in segments:
        u = f"clip((on/{_fmt(metadata['fps'])}-{_fmt(segment.t)})/{_fmt(segment.duration)},0,1)"
        eased = f"(({u})*({u})*(3-2*({u})))"
        delta = segment.end[axis] - segment.start[axis]
        value = f"({_fmt(segment.start[axis])}+({_fmt(delta)})*{eased})" if delta else _fmt(segment.start[axis])
        values.append(value)

    def select(start, end):
        if start == end:
            return values[start]
        middle = (start + end) // 2
        # Balanced lookup keeps parse/evaluation depth logarithmic in event count.
        return (f"if(gte({clock},{_fmt(segments[middle].t)}),"
                f"{select(middle + 1, end)},{select(start, middle)})")

    return select(0, len(values) - 1)


def build_filter(metadata):
    """One absolute-time expression per axis, not a stateful zoom accumulator."""
    w, h = metadata["viewport"]["width"], metadata["viewport"]["height"]
    z = _expression(metadata, 0, 1)
    x = _expression(metadata, 1, w / 2)
    y = _expression(metadata, 2, h / 2)
    fps = _fmt(metadata["fps"])
    return (f"fps={fps},zoompan=z='{z}':"
            f"x='clip(({x})-iw/(2*zoom),0,iw-iw/zoom)':"
            f"y='clip(({y})-ih/(2*zoom),0,ih-ih/zoom)':"
            f"d=1:s={w}x{h}:fps={fps},format=yuv420p")


def probe(video, ffmpeg="ffmpeg", timeout=30):
    binary = Path(ffmpeg)
    sibling = binary.with_name("ffprobe.exe" if binary.suffix.lower() == ".exe" else "ffprobe")
    ffprobe = str(sibling) if sibling.is_file() else "ffprobe"
    command = [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)]
    logger.debug("probe command=%r", command)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=True)
        data = json.loads(result.stdout)
        stream = next(s for s in data["streams"] if s.get("codec_type") == "video")
        duration = float(data["format"].get("duration") or stream.get("duration"))
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("invalid duration")
        return dict(width=int(stream["width"]), height=int(stream["height"]), duration=duration)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration, TypeError) as exc:
        raise ZoomError(f"cannot probe video: {exc}") from exc


def build_command(video, output, metadata, ffmpeg="ffmpeg"):
    return [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(video),
            "-map", "0:v:0", "-map", "0:a?", "-vf", build_filter(metadata),
            "-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-deadline", "good",
            "-cpu-used", "4", "-threads", "2", "-c:a", "libopus", str(output)]


def display_command(command):
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def calibrate_metadata(video, metadata, ffmpeg, timeout=30):
    """Align the capture clock using a known marker, without requiring ffmpeg on capture hosts."""
    if not metadata.get("calibration"):
        return metadata
    command = [str(ffmpeg), "-v", "error", "-nostdin", "-i", str(video), "-t", "3", "-vf",
               "fps=25,crop=2:2:8:8,scale=1:1,format=rgb24", "-f", "rawvideo", "-"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=True)
        pixels = result.stdout
        first = next(i // 3 for i in range(0, len(pixels)-2, 3)
                     if pixels[i] > 200 and pixels[i+1] < 80 and pixels[i+2] > 200)
    except (OSError, subprocess.SubprocessError, StopIteration) as exc:
        raise ZoomError("cannot locate video calibration marker") from exc
    offset = metadata["calibration"]["t"] - first / 25
    corrected = dict(metadata)
    corrected["events"] = [dict(e, t=max(0, e["t"]-offset)) for e in metadata["events"]]
    logger.debug("video clock calibrated offset=%.4fs marker_frame=%d", offset, first)
    return corrected


def process_video(video, output=None, sidecar=None, ffmpeg="ffmpeg", timeout=300, dry_run=False):
    explicit_sidecar = sidecar is not None
    video = Path(video).resolve()
    output = Path(output).resolve() if output else video.with_name(video.stem + ".zoomed.webm")
    sidecar = Path(sidecar) if sidecar is not None else Path(str(video) + ".zoom.json")
    if not video.is_file():
        raise ZoomError(f"video not found: {video}")
    if video.suffix.lower() != ".webm":
        logger.warning("[FIX:zoom-format] rejected non-WebM input: %s", video)
        raise ZoomError("zoom processing requires a WebM input (.webm)")
    if output == video or (output.exists() and os.path.samefile(video, output)):
        raise ZoomError("input and output must differ")
    if output.suffix.lower() != ".webm":
        raise ZoomError("output must have .webm extension")
    if explicit_sidecar and not sidecar.is_file():
        raise ZoomError("explicit sidecar file not found")
    metadata = load_sidecar(sidecar) if sidecar.exists() else None
    if metadata and metadata["events"]:
        command = build_command(video, output, metadata, ffmpeg)
    else:
        command = None
    logger.debug("process start video=%s sidecar=%s output=%s events=%d", video, sidecar, output,
                 len(metadata["events"]) if metadata else 0)
    if dry_run:
        if metadata and metadata.get("calibration") and command:
            print("# Preview uses capture clock; encoding first aligns events to the startup marker.")
        print(display_command(command) if command else f"copy {video} -> {output}")
        return str(output)
    started = time.monotonic()
    if command:
        source = probe(video, ffmpeg, min(timeout, 30))
        metadata = calibrate_metadata(video, metadata, ffmpeg, min(timeout, 30))
        command = build_command(video, output, metadata, ffmpeg)
        if (source["width"], source["height"]) != (metadata["viewport"]["width"], metadata["viewport"]["height"]):
            raise ZoomError("video dimensions do not match sidecar viewport (1:1 required)")
        if metadata["events"][-1]["t"] > source["duration"]:
            raise ZoomError("zoom event is beyond end of video")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{uuid.uuid4().hex}.part.webm")
    filter_path = temporary.with_suffix(".filter")
    try:
        if command:
            command[-1] = str(temporary)
            filter_index = command.index("-vf")
            filter_path.write_text("[0:v:0]" + command[filter_index + 1] + "[zoomed]", encoding="utf-8")
            command[filter_index:filter_index + 2] = ["-/filter_complex", str(filter_path)]
            command[command.index("-map") + 1] = "[zoomed]"
            logger.debug("[FIX:long-filter] events=%d filter=%s command=%r",
                         len(metadata["events"]), filter_path, command)
            logger.debug("encoding command=%r", command)
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
                if result.returncode and "Unrecognized option '/filter_complex'" in result.stderr:
                    # Debian bookworm FFmpeg 5.1 predates generic -/option file loading.
                    command[command.index("-/filter_complex")] = "-filter_complex_script"
                    logger.debug("[FIX:long-filter] retry using legacy filter file option")
                    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise ZoomError(f"ffmpeg timed out after {timeout}s") from exc
            except OSError as exc:
                raise ZoomError(f"cannot run ffmpeg: {exc}") from exc
            if result.returncode:
                logger.error("ffmpeg failed exit=%d stderr=%s", result.returncode, result.stderr[-2000:])
                raise ZoomError("ffmpeg encoding failed; see processing logs")
            target = probe(temporary, ffmpeg, min(timeout, 30))
            if (target["width"], target["height"]) != (source["width"], source["height"]):
                raise ZoomError("encoded dimensions changed")
            if abs(target["duration"] - source["duration"]) > 1 / metadata["fps"] + .005:
                raise ZoomError("encoded duration changed")
        else:
            shutil.copyfile(video, temporary)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
        filter_path.unlink(missing_ok=True)
    logger.info("processed output=%s elapsed=%.3fs", output, time.monotonic() - started)
    return str(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True)
    parser.add_argument("--sidecar")
    parser.add_argument("--output")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ffmpeg", default=os.environ.get("FFMPEG_PATH", "ffmpeg"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, os.environ.get("ZOOM_LOG_LEVEL", "INFO").upper(), logging.INFO))
    try:
        output = process_video(args.video, args.output, args.sidecar, args.ffmpeg, dry_run=args.dry_run)
        if not args.dry_run:
            print(output)
        return 0
    except (ZoomError, OSError) as exc:
        logger.error("processing failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
