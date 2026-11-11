"""Task 2 Docker E2E. Run after docker compose up --build -d --wait.

python scripts/verify_zoom.py --video output.webm
python scripts/verify_zoom.py                    # generates a real testsrc clip inside web
python scripts/verify_zoom.py --restart          # also verifies persisted result after restart

Uses Python stdlib only. Leaves uniquely named verification records in the local stack.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent.parent


def docker(*args, binary=False):
    result = subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True,
                            timeout=420)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-2000:])
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def http(method, url, body=None, headers=None):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=420) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def api(method, url, body=None, headers=None):
    status, raw = http(method, url, body, headers)
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {raw[:300]!r}")
    return json.loads(raw)


def multipart(company, video, sidecar):
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="company_name"\r\n\r\n{company}\r\n'.encode()]
    for field, filename, content, mime in [
        ("file", "verify.webm", video, "video/webm"),
        ("zoom", "verify.webm.zoom.json", json.dumps(sidecar).encode(), "application/json"),
    ]:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode()+content+b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def check(condition, label):
    if not condition:
        raise RuntimeError(label)
    print(f"[PASS] {label}", flush=True)


def db_job(job_id):
    # UUID validated locally; values still passed as SQL parameters inside Python.
    job_id = str(uuid.UUID(job_id))
    code = ("import json,web_service; c=web_service._connect(web_service.db_config_from_env()); "
            "q=c.cursor(); q.execute('SELECT * FROM jobs WHERE id=%s',("+repr(job_id)+",)); "
            "print(json.dumps(dict(q.fetchone()),default=str)); c.close()")
    return json.loads(docker("exec", "-T", "web", "python", "-c", code))


def probe(path):
    return json.loads(docker("exec", "-T", "web", "ffprobe", "-v", "error", "-show_streams",
                             "-show_format", "-of", "json", path))


def video_stream(data):
    return next(s for s in data["streams"] if s["codec_type"] == "video")


def frame_digest(path, t):
    frame = docker("exec", "-T", "web", "ffmpeg", "-v", "error", "-ss", str(t), "-i", path,
                   "-frames:v", "1", "-vf", "scale=160:90", "-f", "rawvideo", "-pix_fmt", "rgb24", "-", binary=True)
    return hashlib.sha256(frame).hexdigest()


def run(args):
    base = args.base_url.rstrip("/")
    api("GET", base+"/health")
    check("ffmpeg version" in docker("exec", "-T", "web", "ffmpeg", "-version"), "ffmpeg installed inside web")
    check("libvpx-vp9" in docker("exec", "-T", "web", "ffmpeg", "-hide_banner", "-encoders"), "VP9 encoder available")
    token = uuid.uuid4().hex
    generated = "/tmp/verify-zoom-"+token+".webm"
    try:
        if args.video:
            video_path = Path(args.video).resolve()
            check(video_path.is_file(), "real input file exists")
            video = video_path.read_bytes()
        else:
            docker("exec", "-T", "web", "ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi",
                   "-i", "testsrc2=size=1280x720:rate=25", "-t", "7", "-c:v", "libvpx-vp9",
                   "-cpu-used", "8", "-threads", "2", generated)
            video = docker("exec", "-T", "web", "cat", generated, binary=True)
            check(len(video) > 1000, "generated valid contrast clip inside Docker")
        events = [dict(t=t, x=x, y=y, type=kind) for t,kind,x,y in [
            (1,"in",640,360), (2,"out",640,360), (3,"in",10,10), (4,"out",10,10), (5,"in",1200,650)]]
        sidecar = dict(viewport=dict(width=1280,height=720), fps=25, zoom_factor=2, ramp_sec=.5, events=events)
        form, headers = multipart("Verify Zoom "+token, video, sidecar)
        uploaded = api("POST", base+"/upload", form, headers)
        job_id = uploaded["job_id"]
        processed = api("POST", base+f"/jobs/{job_id}/process")
        check(processed["status"] == "completed", "two zoom pairs and open-ended zoom processed inside Docker")
        job = db_job(job_id)
        check(job["processed_video_url"] == processed["processed_video_url"], "result URL persisted in PostgreSQL")
        source_meta, result_meta = probe(job["source_path"]), probe(job["processed_file_path"])
        before, after = video_stream(source_meta), video_stream(result_meta)
        check((before["width"],before["height"]) == (after["width"],after["height"]), "resolution preserved")
        check(abs(float(source_meta["format"]["duration"])-float(result_meta["format"]["duration"])) <= .045, "duration preserved within one frame")
        check(frame_digest(job["source_path"],1.7) != frame_digest(job["processed_file_path"],1.7), "encoded zoom frame differs from source")
        check(frame_digest(job["source_path"],6) != frame_digest(job["processed_file_path"],6), "open-ended zoom remains active at end")
        status, downloaded = http("GET", processed["processed_video_url"])
        check(status == 200 and len(downloaded) > 1000, "processed media URL downloads a real video")
        check(api("POST", base+f"/jobs/{job_id}/process")["processed_video_url"] == processed["processed_video_url"], "completed process is idempotent")
        for path in [f"/media/.jobs/{job_id}/input.webm",f"/media/%2Ejobs/{job_id}/input.webm", "/media/.secret.part"]:
            check(http("GET",base+path)[0] in (403,404), "Apache denies "+path)

        sidecar["events"] = []
        form, headers = multipart("Verify Zoom Copy "+token, video, sidecar)
        copy_job = api("POST",base+"/upload",form,headers)["job_id"]
        copied = api("POST",base+f"/jobs/{copy_job}/process")
        check(http("GET",copied["processed_video_url"])[1] == video, "no-events result is a byte-identical copy")
        if args.restart:
            docker("restart", "web", "db")
            # Use Compose health waiting rather than a fixed sleep.
            docker("up", "-d", "--wait")
            check(api("GET",base+f"/jobs/{job_id}")["status"] == "completed", "job survives stack restart")
            check(http("GET",processed["processed_video_url"])[1] == downloaded, "processed file survives restart")
        print(f"[INFO] Playback URL: {processed['processed_video_url']}")
        print("ALL ZOOM CHECKS PASSED")
    finally:
        if not args.video:
            docker("exec", "-T", "web", "rm", "-f", generated)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", help="real 1280x720 WebM at least 6 seconds long")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--restart", action="store_true", help="also restart the local stack without deleting volumes")
    args = parser.parse_args(argv)
    try:
        run(args)
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError, urllib.error.URLError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
