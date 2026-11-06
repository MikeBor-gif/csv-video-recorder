"""End-to-end check of the Docker setup against the Internship Task 1 acceptance checklist.

Run from any folder (Python 3.9+, standard library only; Docker with Compose v2 required):

    python scripts/verify_docker.py

Steps:
  1. docker compose up --build -d --wait   -> both containers start and become healthy
  2. POST /upload                          -> returns a URL that serves the same bytes
  3. GET /get_video_url?company_name=...   -> returns that URL (404 for an unknown name)
  4. unit tests inside the web container
  5. docker compose down + up              -> record and video are still there
  6. web waited for db                     -> no restarts, "database ready" in the log

The script never deletes volumes (no "down -v"). Exit code 0 = all checks passed.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
COMPANY = "Verify Docker GmbH"
HTTP_TIMEOUT = 30
READY_TIMEOUT = 120

_failures = []


def step(title):
    print(f"\n[STEP] {title}", flush=True)


def check(condition, message, details=""):
    if condition:
        print(f"[PASS] {message}", flush=True)
    else:
        print(f"[FAIL] {message}" + (f": {details}" if details else ""), flush=True)
        _failures.append(message)
    return condition


def compose(*args, check_exit=True, quiet=False):
    command = ["docker", "compose", *args]
    print("  $ " + " ".join(command), flush=True)
    result = subprocess.run(command, cwd=str(PROJECT_DIR), capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if not quiet and result.stdout.strip():
        print(_indent(result.stdout), flush=True)
    if check_exit and result.returncode != 0:
        print(_indent(result.stderr), flush=True)
        raise SystemExit(_finish(f"command failed: {' '.join(command)}"))
    return result


def _indent(text, limit=40):
    lines = text.strip().splitlines()[-limit:]
    return "\n".join("    " + line for line in lines)


def http(method, url, body=None, headers=None):
    """Return (status, headers, bytes); HTTP errors are returned, not raised."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        # Service not reachable: report a failed check instead of a traceback.
        reason = getattr(exc, "reason", exc)
        return 0, {}, f"connection failed: {reason}".encode("utf-8")


def http_json(method, url, body=None, headers=None):
    status, _headers, data = http(method, url, body, headers)
    try:
        return status, json.loads(data.decode("utf-8"))
    except ValueError:
        return status, {"raw": data[:200].decode("utf-8", "replace")}


def multipart(fields, file_field, filename, content, content_type="video/webm"):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode("utf-8")
        + content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def wait_until_ready(base_url):
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        try:
            status, body = http_json("GET", base_url + "/health")
            if status == 200 and body.get("status") == "ok":
                return True
        except OSError:
            pass
        time.sleep(2)
    return False


def container_state(service):
    container_id = compose("ps", "-q", service, quiet=True).stdout.strip()
    if not container_id:
        return {}
    result = subprocess.run(
        ["docker", "inspect", "-f",
         "{{json .State}}|{{.RestartCount}}", container_id],
        capture_output=True, text=True,
    )
    state_json, _, restarts = result.stdout.strip().rpartition("|")
    state = json.loads(state_json) if state_json else {}
    state["RestartCount"] = int(restarts or 0)
    return state


def check_containers(label):
    for service in ("web", "db"):
        state = container_state(service)
        health = (state.get("Health") or {}).get("Status")
        check(state.get("Running") is True and health == "healthy",
              f"{label}: container '{service}' is running and healthy",
              f"running={state.get('Running')} health={health}")


def check_record(base_url, expected_url, expected_bytes, label):
    query = urllib.parse.urlencode({"company_name": COMPANY})
    status, body = http_json("GET", f"{base_url}/get_video_url?{query}")
    check(status == 200 and body.get("video_url") == expected_url,
          f"{label}: GET /get_video_url returns the uploaded URL", f"HTTP {status} {body}")
    status, headers, data = http("GET", expected_url)
    check(status == 200 and data == expected_bytes,
          f"{label}: the video URL serves the uploaded file",
          f"HTTP {status}, {len(data)} bytes, expected {len(expected_bytes)}")
    check((headers.get("Content-Type") or "").startswith("video/webm"),
          f"{label}: Content-Type is video/webm", headers.get("Content-Type"))


def _finish(reason=""):
    if reason:
        _failures.append(reason)
    if _failures:
        print("\n--- docker compose logs (tail) ---")
        result = compose("logs", "--tail", "50", "web", "db", check_exit=False, quiet=True)
        print(_indent(result.stdout + result.stderr, limit=100))
        print(f"\nFAILED: {len(_failures)} check(s)")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--skip-build", action="store_true", help="do not pass --build")
    parser.add_argument("--skip-tests", action="store_true", help="do not run unit tests")
    parser.add_argument("--down-after", action="store_true",
                        help="stop the containers at the end (volumes are kept)")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    step("1. docker compose up --build starts both containers")
    up = ["up", "-d", "--wait"] + ([] if args.skip_build else ["--build"])
    compose(*up, quiet=True)
    check_containers("start")
    check(wait_until_ready(base_url), "web answers GET /health with status ok")
    status, body = http_json("GET", base_url + "/")
    check(status == 200, "GET / answers", f"HTTP {status} {body}")

    step("2. POST /upload uploads a video and returns a working URL")
    video = b"\x1aE\xdf\xa3" + os.urandom(64 * 1024)  # EBML magic + random payload
    form, headers = multipart({"company_name": COMPANY}, "file", "verify.webm", video)
    status, body = http_json("POST", base_url + "/upload", form, headers)
    video_url = body.get("video_url", "")
    check(status == 200 and body.get("company_name") == COMPANY and video_url,
          "POST /upload returns company_name and video_url", f"HTTP {status} {body}")
    if not video_url:
        return _finish("upload did not return a URL")
    print(f"  video_url = {video_url}")

    step("3. GET /get_video_url returns the URL")
    check_record(base_url, video_url, video, "before restart")
    status, body = http_json("GET", base_url + "/get_video_url?company_name=No+Such+Company")
    check(status == 404, "unknown company -> 404", f"HTTP {status} {body}")

    if not args.skip_tests:
        step("4. unit tests inside the web container")
        result = compose("exec", "-T", "-e", "WEB_SERVICE_LOG_LEVEL=", "web",
                         "python", "-m", "unittest", "discover", "-s", "tests",
                         check_exit=False, quiet=True)
        print(_indent(result.stderr, limit=6))
        check(result.returncode == 0, "unit tests pass (python -m unittest in container)")

    step("5. after docker compose down and up, data and videos are preserved")
    compose("down", quiet=True)
    compose("up", "-d", "--wait", quiet=True)
    check_containers("restart")
    check(wait_until_ready(base_url), "web is ready again after restart")
    check_record(base_url, video_url, video, "after restart")

    step("6. web waits for db and does not crash on startup")
    state = container_state("web")
    check(state.get("RestartCount") == 0, "web container did not restart",
          f"RestartCount={state.get('RestartCount')}")
    logs = compose("logs", "web", quiet=True).stdout
    check("database ready" in logs, "web log contains 'database ready'")
    check("Traceback" not in logs, "web log has no Python tracebacks")

    print(f"\n[NOTE] the test record '{COMPANY}' and its video stay in the database "
          "and the uploads volume (re-runs overwrite them).")

    if args.down_after:
        step("stop containers (volumes are kept)")
        compose("down", quiet=True)

    return _finish()


if __name__ == "__main__":
    sys.exit(main())
