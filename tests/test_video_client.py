"""Tests for video_client.py against a live web_service running in a background thread.

PreviewVideo opens a visible browser and is verified manually, not here.
Run: python -m unittest discover -s tests -v
"""

import inspect
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import requests  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

import video_client  # noqa: E402
import web_service  # noqa: E402


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class VideoClientTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("WEB_SERVICE_LOG_LEVEL"):
            logging.disable(logging.CRITICAL)
        cls.tmp = Path(tempfile.mkdtemp(prefix="video_client_test_"))
        app = web_service.create_app(db_path=cls.tmp / "videos.db",
                                     upload_dir=cls.tmp / "uploads")
        cls.server = make_server("127.0.0.1", 0, app, threaded=True)
        cls.server_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        cls.video_path = cls.tmp / "Sample Company.webm"
        cls.video_bytes = os.urandom(2048)
        cls.video_path.write_bytes(cls.video_bytes)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        logging.disable(logging.NOTSET)

    def test_signatures_match_task(self):
        self.assertEqual(
            str(inspect.signature(video_client.UploadVideo)),
            "(server_url: str, video_path: str, company_name: str) -> str",
        )
        self.assertEqual(
            str(inspect.signature(video_client.GetVideoURL)),
            "(server_url: str, company_name: str) -> str",
        )

    def test_upload_returns_playable_url(self):
        url = video_client.UploadVideo(self.server_url, str(self.video_path), "Upload GmbH")
        self.assertEqual(url, f"{self.server_url}/media/Upload_GmbH.webm")
        response = requests.get(url, timeout=10)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "video/webm")
        self.assertEqual(response.content, self.video_bytes)

    def test_get_video_url_returns_uploaded_url(self):
        uploaded = video_client.UploadVideo(self.server_url + "/", str(self.video_path), "Lookup AG")
        self.assertEqual(video_client.GetVideoURL(self.server_url, "Lookup AG"), uploaded)

    def test_get_video_url_unknown_company_raises(self):
        with self.assertRaisesRegex(video_client.VideoClientError, "company not found"):
            video_client.GetVideoURL(self.server_url, "Unknown Company")

    def test_upload_missing_file_raises(self):
        with self.assertRaisesRegex(video_client.VideoClientError, "not found"):
            video_client.UploadVideo(self.server_url, str(self.tmp / "missing.webm"), "X")

    def test_upload_rejected_by_server_raises(self):
        bad = self.tmp / "notes.txt"
        bad.write_text("not a video")
        with self.assertRaisesRegex(video_client.VideoClientError, "HTTP 400"):
            video_client.UploadVideo(self.server_url, str(bad), "Bad File")

    def test_unreachable_server_raises(self):
        dead_url = f"http://127.0.0.1:{_free_port()}"
        with self.assertRaises(video_client.VideoClientError):
            video_client.UploadVideo(dead_url, str(self.video_path), "Offline")
        with self.assertRaises(video_client.VideoClientError):
            video_client.GetVideoURL(dead_url, "Offline")

    def test_import_does_not_load_requests_or_playwright(self):
        code = ("import sys, video_client; "
                "print('requests' in sys.modules, 'playwright' in sys.modules)")
        output = subprocess.run(
            [sys.executable, "-c", code], cwd=str(PROJECT_DIR),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(output, "False False")

    def test_preview_rejects_negative_seconds(self):
        with self.assertRaises(video_client.VideoClientError):
            video_client.PreviewVideo("http://127.0.0.1/none.webm", -1)


if __name__ == "__main__":
    unittest.main()
