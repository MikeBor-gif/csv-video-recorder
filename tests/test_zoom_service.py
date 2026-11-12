import io
import json
from pathlib import Path
from unittest.mock import patch
import uuid
import threading

import pytest

import web_service
from tests import pg_helpers
from tests.test_zoom_events import event, metadata
from zoom_events import ZoomError


@pytest.fixture
def service(tmp_path):
    config = pg_helpers.test_db_config()
    pg_helpers.reset_videos_table(config)
    app = web_service.create_app(config, tmp_path / "uploads")
    return app, app.test_client(), config


def upload(client, events=None, content=b"original", company="Zoom AG", raw=None, filename="input.webm"):
    data = dict(company_name=company, file=(io.BytesIO(content), filename))
    if events is not None or raw is not None:
        data["zoom"] = (io.BytesIO(raw if raw is not None else json.dumps(metadata(events)).encode()), "events.json")
    return client.post("/upload", data=data, content_type="multipart/form-data")


def test_upload_snapshot_is_immutable(service):
    app, client, config = service
    first = upload(client, [event(0)])
    assert first.status_code == 200
    job_id = first.json["job_id"]
    from pathlib import Path
    source = pg_helpers.query(config, "SELECT source_path FROM jobs WHERE id=%s", (job_id,))[0][0]
    upload(client, content=b"replacement")
    assert Path(source).read_bytes() == b"original"
    assert client.get(first.json["video_url"]).data == b"replacement"


def test_bad_sidecar_preserves_previous_upload(service):
    _, client, config = service
    first = upload(client)
    rejected = upload(client, raw=b"invalid", content=b"bad")
    assert rejected.status_code == 400
    assert client.get(first.json["video_url"]).data == b"original"
    assert pg_helpers.query(config, "SELECT count(*) FROM jobs")[0][0] == 0


def test_plain_upload_keeps_old_contract(service):
    _, client, _ = service
    assert set(upload(client).json) == {"company_name", "video_url"}


@pytest.mark.parametrize("filename", ["input.mp4", "input.mov", "input.ogg"])
def test_zoom_rejects_non_webm_but_plain_upload_still_works(service, filename):
    _, client, config = service
    first = upload(client, filename=filename)
    assert first.status_code == 200
    rejected = upload(client, events=[], content=b"bad", filename=filename)
    assert rejected.status_code == 400
    assert client.get(first.json["video_url"]).data == b"original"
    assert pg_helpers.query(config, "SELECT count(*) FROM jobs")[0][0] == 0


def test_excessive_factor_preserves_previous_upload(service):
    _, client, config = service
    first = upload(client)
    data = metadata([event(0)])
    data["zoom_factor"] = 20
    assert upload(client, raw=json.dumps(data).encode(), content=b"bad").status_code == 400
    assert client.get(first.json["video_url"]).data == b"original"
    assert pg_helpers.query(config, "SELECT count(*) FROM jobs")[0][0] == 0


def fake_process(video, output, **kwargs):
    Path(output).write_bytes(Path(video).read_bytes() + b" zoomed")
    return str(output)


def test_processing_and_idempotency(service):
    _, client, config = service
    job = upload(client, [event(0)]).json["job_id"]
    assert client.get(f"/jobs/{job}").json["status"] == "pending"
    with patch("web_service.process_video", side_effect=fake_process) as process:
        response = client.post(f"/jobs/{job}/process")
        assert response.status_code == 200
        assert response.json["status"] == "completed"
        assert client.post(f"/jobs/{job}/process").json == response.json
        assert process.call_count == 1
    assert client.get(response.json["processed_video_url"]).data == b"original zoomed"
    assert pg_helpers.query(config, "SELECT status FROM jobs WHERE id=%s", (job,))[0][0] == "completed"


def test_failure_retry_and_safe_error(service):
    _, client, _ = service
    job = upload(client, [event(0)]).json["job_id"]
    with patch("web_service.process_video", side_effect=ZoomError("secret /app/path")):
        response = client.post(f"/jobs/{job}/process")
    assert response.status_code == 500
    assert "secret" not in response.get_data(as_text=True)
    assert client.get(f"/jobs/{job}").json["status"] == "failed"
    with patch("web_service.process_video", side_effect=fake_process):
        assert client.post(f"/jobs/{job}/process").status_code == 200


def test_timeout_and_unknown(service):
    _, client, _ = service
    assert client.post(f"/jobs/{uuid.uuid4()}/process").status_code == 404
    job = upload(client, [event(0)]).json["job_id"]
    with patch("web_service.process_video", side_effect=ZoomError("ffmpeg timed out")):
        assert client.post(f"/jobs/{job}/process").status_code == 504


def test_pending_conflict_and_deadline_recovery(service):
    app, client, config = service
    job = upload(client, [event(0)]).json["job_id"]
    with app.app_context():
        db = web_service.get_db()
        with db.cursor() as cur:
            cur.execute("UPDATE jobs SET status='processing',deadline_at=NOW()+INTERVAL '1 hour' WHERE id=%s", (job,))
        db.commit()
    with patch("web_service.process_video") as process:
        assert client.post(f"/jobs/{job}/process").status_code == 409
        process.assert_not_called()
    with app.app_context():
        db = web_service.get_db()
        with db.cursor() as cur:
            cur.execute("UPDATE jobs SET deadline_at=NOW()-INTERVAL '1 hour' WHERE id=%s", (job,))
        db.commit()
    assert client.get(f"/jobs/{job}").json["status"] == "failed"
    with patch("web_service.process_video", side_effect=fake_process):
        assert client.post(f"/jobs/{job}/process").status_code == 200


@pytest.mark.parametrize("url", ["/media/.jobs/a/input.webm", "/media/%2Ejobs/a/input.webm", "/media/.secret.part"])
def test_private_files_are_not_served(service, url):
    _, client, _ = service
    assert client.get(url).status_code == 404


def test_concurrent_process_claim_is_exclusive(service):
    app, client, _ = service
    job = upload(client, [event(0)]).json["job_id"]
    entered, release = threading.Event(), threading.Event()
    responses = []
    def blocking_process(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return fake_process(*args, **kwargs)
    def request_process():
        with app.test_client() as other:
            responses.append(other.post(f"/jobs/{job}/process").status_code)
    with patch("web_service.process_video", side_effect=blocking_process) as process:
        thread = threading.Thread(target=request_process)
        thread.start()
        try:
            assert entered.wait(10)
            assert client.post(f"/jobs/{job}/process").status_code == 409
        finally:
            release.set()
            thread.join(timeout=10)
        assert responses == [200]
        assert process.call_count == 1


def test_lost_attempt_cannot_publish(service):
    app, client, config = service
    job = upload(client, [event(0)]).json["job_id"]
    def expire_current(*args, **kwargs):
        fake_process(*args, **kwargs)
        with web_service._connect(config) as db:
            with db.cursor() as cur:
                cur.execute("UPDATE jobs SET status='failed',attempt_id=%s WHERE id=%s", (str(uuid.uuid4()), job))
        return str(kwargs["output"])
    with patch("web_service.process_video", side_effect=expire_current):
        assert client.post(f"/jobs/{job}/process").status_code == 409
    assert not list(Path(app.config["UPLOAD_DIR"]).glob("*.zoomed.webm"))


def test_database_failure_restores_previous_upload(service):
    _, client, config = service
    original = upload(client)
    real_upsert = web_service.upsert_video
    def fail_after_sql(*args, **kwargs):
        real_upsert(*args, **kwargs)
        raise OSError("simulated filesystem failure")
    with patch("web_service.upsert_video", side_effect=fail_after_sql):
        assert upload(client, [event(0)], content=b"replacement").status_code == 500
    assert client.get(original.json["video_url"]).data == b"original"
    assert pg_helpers.query(config, "SELECT count(*) FROM jobs")[0][0] == 0


@pytest.mark.parametrize("name", ["videos", "postgres", "template0", "template1"])
def test_test_database_cannot_target_application_or_system_database(monkeypatch, name):
    for key, value in dict(DB_HOST="localhost", DB_PORT="5432", DB_NAME="videos",
                           DB_USER="test", DB_PASSWORD="test", TEST_DB_NAME=name).items():
        monkeypatch.setenv(key, value)
    with patch.object(pg_helpers, "_ensure_database") as ensure:
        with pytest.raises(RuntimeError, match="separate"):
            pg_helpers.test_db_config()
        ensure.assert_not_called()
