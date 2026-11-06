"""Tests for web_service.py (Flask test client, PostgreSQL test database, temporary uploads).

Run inside the container: docker compose exec -T web python -m unittest discover -s tests -v
"""

import io
import logging
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web_service  # noqa: E402
from tests import pg_helpers  # noqa: E402

SCHEMA_COLUMNS = ["id", "company_name", "file_path", "video_url", "created_at", "updated_at"]


def setUpModule():
    if not os.environ.get("WEB_SERVICE_LOG_LEVEL"):
        logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class WebServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="web_service_test_"))
        self.db_config = pg_helpers.test_db_config()
        pg_helpers.reset_videos_table(self.db_config)
        self.upload_dir = self.tmp / "uploads"
        self.app = web_service.create_app(db_config=self.db_config, upload_dir=self.upload_dir)
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers ---------------------------------------------------------------

    def upload(self, company_name, content=b"webm-bytes", filename="video.webm", client=None):
        data = {"company_name": company_name}
        if content is not None:
            data["file"] = (io.BytesIO(content), filename)
        return (client or self.client).post(
            "/upload", data=data, content_type="multipart/form-data"
        )

    def query(self, sql, params=()):
        return pg_helpers.query(self.db_config, sql, params)

    # startup ---------------------------------------------------------------

    def test_start_creates_table_and_uploads_folder(self):
        self.assertEqual(self.query("SELECT to_regclass('videos') IS NOT NULL")[0][0], True)
        self.assertTrue(self.upload_dir.is_dir())

    def test_schema_matches_task(self):
        columns = self.query(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'videos' ORDER BY ordinal_position"
        )
        self.assertEqual([name for name, _ in columns], SCHEMA_COLUMNS)
        self.assertEqual(dict(columns)["created_at"], "timestamp with time zone")
        unique = self.query(
            "SELECT COUNT(*) FROM pg_indexes WHERE tablename = 'videos' "
            "AND indexdef LIKE '%%UNIQUE%%(company_name)%%'"
        )[0][0]
        self.assertEqual(unique, 1)

    # POST /upload ----------------------------------------------------------

    def test_upload_returns_url_and_stores_record(self):
        response = self.upload("ACME GmbH", b"first")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {
            "company_name": "ACME GmbH",
            "video_url": "http://localhost/media/ACME_GmbH.webm",
        })
        rows = self.query("SELECT company_name, file_path, video_url FROM videos")
        self.assertEqual(len(rows), 1)
        company_name, file_path, video_url = rows[0]
        self.assertEqual(company_name, "ACME GmbH")
        self.assertTrue(os.path.isabs(file_path))
        self.assertEqual(Path(file_path), self.upload_dir.resolve() / "ACME_GmbH.webm")
        self.assertEqual(Path(file_path).read_bytes(), b"first")
        self.assertEqual(video_url, response.json["video_url"])

    def test_database_stores_no_video_bytes(self):
        self.upload("ACME GmbH", b"x" * 4096)
        types = self.query(
            "SELECT pg_typeof(file_path)::text, pg_typeof(video_url)::text, length(file_path) "
            "FROM videos"
        )[0]
        self.assertEqual(types[:2], ("text", "text"))
        self.assertLess(types[2], 4096)

    def test_reupload_updates_record_without_duplicates(self):
        self.upload("ACME GmbH", b"first")
        created = self.query("SELECT id, created_at FROM videos")[0]
        response = self.upload("ACME GmbH", b"second")
        self.assertEqual(response.status_code, 200)
        rows = self.query("SELECT id, created_at, updated_at FROM videos")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:2], created)
        self.assertGreaterEqual(rows[0][2], rows[0][1])
        self.assertEqual((self.upload_dir / "ACME_GmbH.webm").read_bytes(), b"second")

    def test_reupload_with_other_extension_removes_previous_file(self):
        self.upload("ACME GmbH", b"webm", filename="a.webm")
        response = self.upload("ACME GmbH", b"mp4", filename="a.mp4")
        self.assertTrue(response.json["video_url"].endswith("/media/ACME_GmbH.mp4"))
        self.assertFalse((self.upload_dir / "ACME_GmbH.webm").exists())
        self.assertEqual(self.query("SELECT COUNT(*) FROM videos")[0][0], 1)

    def test_names_differing_only_in_case_get_separate_files(self):
        first = self.upload("ACME", b"upper").json["video_url"]
        second = self.upload("acme", b"lower").json["video_url"]
        self.assertNotEqual(first.lower(), second.lower())
        for url, expected in ((first, b"upper"), (second, b"lower")):
            response = self.client.get(url)
            self.assertEqual(response.data, expected)
            response.close()

    def test_unsafe_characters_are_removed_from_file_name(self):
        cases = {"A\\B": "A_B", "Müller & Söhne": "Muller__Sohne", "../etc": "etc", "CON": "_CON"}
        for company, stem in cases.items():
            with self.subTest(company):
                url = self.upload(company).json["video_url"]
                self.assertTrue(url.endswith(f"/media/{stem}.webm"), url)

    def test_non_ascii_company_name_gets_hashed_file_name(self):
        response = self.upload("ООО Ромашка")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["company_name"], "ООО Ромашка")
        self.assertRegex(response.json["video_url"], r"/media/video_[0-9a-f]{8}\.webm$")

    def test_colliding_file_names_do_not_overwrite_each_other(self):
        first = self.upload("A/B", b"slash").json["video_url"]
        second = self.upload("A B", b"space").json["video_url"]
        self.assertNotEqual(first, second)
        for url, expected in ((first, b"slash"), (second, b"space")):
            response = self.client.get(url)
            self.assertEqual(response.data, expected)
            response.close()

    def test_upload_validation_errors(self):
        cases = {
            "missing file": self.upload("ACME", content=None),
            "empty company": self.upload("   "),
            "bad extension": self.upload("ACME", filename="evil.exe"),
        }
        for name, response in cases.items():
            with self.subTest(name):
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json)
        self.assertEqual(self.query("SELECT COUNT(*) FROM videos")[0][0], 0)

    def test_file_without_extension_defaults_to_webm(self):
        response = self.upload("ACME", filename="video")
        self.assertTrue(response.json["video_url"].endswith("/media/ACME.webm"))

    def test_too_large_upload_returns_413(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 64
        response = self.upload("ACME", b"x" * 1024)
        self.assertEqual(response.status_code, 413)
        self.assertIn("error", response.json)

    def test_concurrent_uploads_do_not_lock_database(self):
        statuses = []

        def worker(index):
            client = self.app.test_client()
            response = self.upload(f"Company {index}", b"x" * 1000, client=client)
            statuses.append(response.status_code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(statuses, [200] * 8)
        self.assertEqual(self.query("SELECT COUNT(*) FROM videos")[0][0], 8)

    # GET /get_video_url ----------------------------------------------------

    def test_get_video_url_returns_stored_url(self):
        uploaded = self.upload("ACME GmbH").json
        response = self.client.get("/get_video_url", query_string={"company_name": "ACME GmbH"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, uploaded)

    def test_get_video_url_unknown_company_returns_404(self):
        response = self.client.get("/get_video_url", query_string={"company_name": "Nobody"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json["company_name"], "Nobody")

    def test_get_video_url_without_name_returns_400(self):
        self.assertEqual(self.client.get("/get_video_url").status_code, 400)

    # GET /media/<filename> -------------------------------------------------

    def test_media_serves_uploaded_file(self):
        url = self.upload("ACME GmbH", b"video-content").json["video_url"]
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "video/webm")
        self.assertEqual(response.data, b"video-content")
        response.close()

    def test_media_supports_range_requests(self):
        url = self.upload("ACME", b"0123456789").json["video_url"]
        response = self.client.get(url, headers={"Range": "bytes=2-5"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"2345")
        response.close()

    def test_media_rejects_unknown_and_traversal_paths(self):
        for path in ("/media/missing.webm", "/media/../videos.db", "/media/..%2Fvideos.db"):
            with self.subTest(path):
                self.assertEqual(self.client.get(path).status_code, 404)

    # GET /videos -----------------------------------------------------------

    def test_videos_lists_records(self):
        self.upload("ACME GmbH")
        self.upload("Berlin Shop")
        records = self.client.get("/videos").json["videos"]
        self.assertEqual({r["company_name"] for r in records}, {"ACME GmbH", "Berlin Shop"})
        self.assertEqual(set(records[0]), {"company_name", "file_path", "video_url",
                                           "created_at", "updated_at"})

    def test_videos_dates_are_iso_strings(self):
        self.upload("ACME GmbH")
        record = self.client.get("/videos").json["videos"][0]
        self.assertRegex(record["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    # service endpoints -----------------------------------------------------

    def test_health_reports_database_ok(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"status": "ok"})

    def test_index_lists_endpoints(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("POST /upload", response.json["endpoints"])

    def test_unknown_route_returns_json_404(self):
        response = self.client.get("/nope")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.json)


class DatabaseSettingsTestCase(unittest.TestCase):
    """Settings and startup behaviour that need no running database."""

    VARIABLES = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")

    def setUp(self):
        self.saved = {name: os.environ.get(name) for name in self.VARIABLES}
        os.environ.update({"DB_HOST": "db", "DB_NAME": "videos",
                           "DB_USER": "video_user", "DB_PASSWORD": "secret"})
        os.environ.pop("DB_PORT", None)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_config_is_read_from_environment(self):
        self.assertEqual(web_service.db_config_from_env(), {
            "host": "db", "port": 5432, "dbname": "videos",
            "user": "video_user", "password": "secret",
        })

    def test_missing_variable_raises(self):
        del os.environ["DB_HOST"]
        with self.assertRaisesRegex(RuntimeError, "DB_HOST"):
            web_service.db_config_from_env()

    def test_invalid_port_raises(self):
        os.environ["DB_PORT"] = "abc"
        with self.assertRaisesRegex(RuntimeError, "DB_PORT"):
            web_service.db_config_from_env()

    def test_wsgi_app_does_not_repeat_full_wait_after_failed_start(self):
        # Apache re-imports the .wsgi script on every request after a failed start;
        # each of those requests must not block for the whole startup wait again.
        os.environ.update({"DB_CONNECT_RETRIES": "3", "DB_CONNECT_DELAY": "0"})
        self.addCleanup(os.environ.pop, "DB_CONNECT_RETRIES", None)
        self.addCleanup(os.environ.pop, "DB_CONNECT_DELAY", None)
        self.addCleanup(setattr, web_service, "_wsgi_app", None)
        self.addCleanup(setattr, web_service, "_startup_failed", False)
        attempts = []

        def refuse(config):
            attempts.append(config["host"])
            raise web_service.psycopg2.OperationalError("connection refused")

        original_connect = web_service._connect
        web_service._connect = refuse
        self.addCleanup(setattr, web_service, "_connect", original_connect)

        with self.assertRaises(RuntimeError):
            web_service.app  # first start: full wait (3 attempts), then failure
        self.assertEqual(len(attempts), 3)
        with self.assertRaises(RuntimeError):
            web_service.app  # next request: one quick attempt, not another full wait
        self.assertEqual(len(attempts), 4)

    def test_close_db_closes_connection_when_rollback_fails(self):
        class BrokenConnection:
            closed = False

            def rollback(self):
                raise web_service.psycopg2.InterfaceError("connection already closed")

            def close(self):
                self.closed = True

        connection = BrokenConnection()
        app = web_service.Flask(__name__)
        with app.app_context():
            web_service.g.db = connection
            web_service.close_db(RuntimeError("request failed"))
        self.assertTrue(connection.closed)

    def test_wait_for_db_gives_up_after_retries(self):
        config = {"host": "127.0.0.1", "port": 1, "dbname": "x", "user": "x", "password": "x"}
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            web_service.wait_for_db(config, retries=2, delay=0)


if __name__ == "__main__":
    unittest.main()
