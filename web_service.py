"""Web service that stores recorded videos and looks them up by company name.

Run in Docker (Apache + mod_wsgi, PostgreSQL):  docker compose up --build
Run locally (Flask dev server):                 python web_service.py

Endpoints:
    POST /upload                         UploadVideo: multipart ``file`` + ``company_name``
    GET  /get_video_url?company_name=... GetVideoURL: 200 JSON or 404
    GET  /media/<filename>               serves an uploaded video file
    GET  /videos                         lists all records
    GET  /health                         database check for the container healthcheck
    GET  /                               short service description

Storage: video files live in the uploads folder; the PostgreSQL table ``videos``
keeps only the company name, the absolute file path and the public URL --
never the video bytes.

Environment variables:
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
                           PostgreSQL connection (required; DB_PORT defaults to 5432)
    DB_CONNECT_RETRIES     attempts to reach the database on startup (default 30)
    DB_CONNECT_DELAY       seconds between attempts (default 2)
    UPLOAD_DIR             folder for video files (default: uploads/ next to this file)
    FLASK_HOST             bind address of the dev server (default 0.0.0.0)
    FLASK_PORT             port of the dev server (default 8000)
    WEB_SERVICE_LOG_LEVEL  DEBUG, INFO, WARNING, ERROR (default INFO)
    MAX_UPLOAD_MB          maximum upload size in megabytes (default 512)
"""

import hashlib
import logging
import mimetypes
import os
import re
import sys
import time
import unicodedata
import uuid
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, current_app, g, jsonify, request, send_from_directory

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_UPLOAD_DIR = BASE_DIR / "uploads"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_MAX_UPLOAD_MB = 512

DEFAULT_DB_PORT = 5432
DEFAULT_DB_CONNECT_RETRIES = 30
DEFAULT_DB_CONNECT_DELAY = 2.0
DB_CONNECT_TIMEOUT_SECONDS = 5
REQUIRED_DB_VARIABLES = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")

ALLOWED_EXTENSIONS = {".webm", ".mp4", ".ogg", ".mov"}
DEFAULT_EXTENSION = ".webm"

# Status codes answered with a JSON body instead of Flask's HTML error page.
JSON_ERROR_CODES = (400, 404, 405, 413, 500)

_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS videos (
    id           SERIAL      PRIMARY KEY,
    company_name TEXT        NOT NULL UNIQUE,
    file_path    TEXT        NOT NULL,  -- absolute path to the file on disk
    video_url    TEXT        NOT NULL,  -- public URL to the video
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# Some systems do not know the .webm type; browsers need it to play the file inline.
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/ogg", ".ogg")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/quicktime", ".mov")

logger = logging.getLogger("web_service")

# ---------------------------------------------------------------------------
# Settings from the environment
# ---------------------------------------------------------------------------


def _env_number(name, default, cast):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = cast(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using %s", name, raw, default)
        return default
    if value < 0:
        logger.warning("negative %s=%r; using %s", name, raw, default)
        return default
    return value


def db_config_from_env() -> dict:
    """Read the PostgreSQL connection settings from DB_* environment variables."""
    missing = [name for name in REQUIRED_DB_VARIABLES if not os.environ.get(name)]
    if missing:
        raise RuntimeError("missing environment variable(s): " + ", ".join(missing))
    raw_port = os.environ.get("DB_PORT") or str(DEFAULT_DB_PORT)
    try:
        port = int(raw_port)
    except ValueError:
        raise RuntimeError(f"invalid DB_PORT={raw_port!r}: must be an integer") from None
    return {
        "host": os.environ["DB_HOST"],
        "port": port,
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }


def _describe(config) -> str:
    """Connection target for logs -- never includes the password."""
    return f"{config['user']}@{config['host']}:{config['port']}/{config['dbname']}"


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------


def _connect(config):
    return psycopg2.connect(
        host=config["host"],
        port=config["port"],
        dbname=config["dbname"],
        user=config["user"],
        password=config["password"],
        connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
        cursor_factory=RealDictCursor,
    )


def wait_for_db(config, retries=None, delay=None):
    """Return an open connection once PostgreSQL answers; raise RuntimeError after ``retries``."""
    if retries is None:
        retries = _env_number("DB_CONNECT_RETRIES", DEFAULT_DB_CONNECT_RETRIES, int)
    if delay is None:
        delay = _env_number("DB_CONNECT_DELAY", DEFAULT_DB_CONNECT_DELAY, float)
    attempts = max(1, retries)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            connection = _connect(config)
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            logger.info("database ready after %d attempt(s): %s", attempt, _describe(config))
            return connection
        except psycopg2.OperationalError as exc:
            last_error = exc
            logger.warning("database not ready (attempt %d/%d): %s",
                           attempt, attempts, str(exc).strip())
            if attempt < attempts:
                time.sleep(delay)
    logger.error("database unavailable after %d attempt(s): %s", attempts, _describe(config))
    raise RuntimeError(f"database {_describe(config)} is unavailable: {last_error}")


def init_db(config, retries=None):
    """Wait for PostgreSQL and create the ``videos`` table if it does not exist."""
    connection = wait_for_db(config, retries=retries)
    try:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA_SQL)
        connection.commit()
    finally:
        connection.close()
    logger.info("DB initialised: %s", _describe(config))


def get_db():
    """Return the PostgreSQL connection of the current request (one per request)."""
    if "db" not in g:
        g.db = _connect(current_app.config["DB_CONFIG"])
        logger.debug("DB connection opened")
    return g.db


def close_db(exc=None):
    connection = g.pop("db", None)
    if connection is None:
        return
    try:
        if exc is not None:
            connection.rollback()
    except psycopg2.Error as rollback_error:
        logger.warning("[FIX] rollback failed on a broken connection: %s",
                       str(rollback_error).strip())
    finally:
        connection.close()
    logger.debug("DB connection closed")


def upsert_video(db, company_name, file_path, video_url):
    """Insert a record or update the existing one for ``company_name``."""
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO videos (company_name, file_path, video_url)
            VALUES (%s, %s, %s)
            ON CONFLICT (company_name) DO UPDATE SET
                file_path  = EXCLUDED.file_path,
                video_url  = EXCLUDED.video_url,
                updated_at = NOW()
            """,
            (company_name, file_path, video_url),
        )
    db.commit()
    logger.debug("DB upsert company=%r file_path=%r", company_name, file_path)


def find_video(db, company_name: str) -> Optional[dict]:
    with db.cursor() as cursor:
        cursor.execute("SELECT * FROM videos WHERE company_name = %s", (company_name,))
        row = cursor.fetchone()
    logger.debug("DB find company=%r found=%s", company_name, row is not None)
    return row


def list_videos(db) -> List[dict]:
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT company_name, file_path, video_url, created_at, updated_at "
            "FROM videos ORDER BY updated_at DESC, id DESC"
        )
        rows = cursor.fetchall()
    records = []
    for row in rows:
        record = dict(row)
        record["created_at"] = record["created_at"].isoformat()
        record["updated_at"] = record["updated_at"].isoformat()
        records.append(record)
    return records


def _owner_of_file(db, file_path: str) -> Optional[str]:
    # Case-insensitive: on Windows/macOS "ACME.webm" and "acme.webm" are the same file.
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT company_name FROM videos WHERE lower(file_path) = lower(%s)", (file_path,)
        )
        row = cursor.fetchone()
    return row["company_name"] if row else None


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------


def _safe_name(name: str) -> str:
    """ASCII-only, filesystem-safe file stem: "ACME GmbH" -> "ACME_GmbH"; may return ""."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"[\\/]", " ", ascii_name)
    ascii_name = "_".join(ascii_name.split())
    ascii_name = re.sub(r"[^A-Za-z0-9_.-]", "", ascii_name).strip("._")
    if ascii_name.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        ascii_name = f"_{ascii_name}"
    return ascii_name


def _name_hash(company_name):
    return hashlib.sha1(company_name.encode("utf-8")).hexdigest()[:8]


def _storage_filename(db, upload_dir, company_name, extension):
    """Build a safe file name for the company that never clobbers another company's file.

    "ACME GmbH" -> "ACME_GmbH.webm"; names that sanitise to nothing (e.g. Cyrillic)
    or collide with another company's file get a short hash of the name appended.
    """
    base = _safe_name(company_name)
    if not base:
        return f"video_{_name_hash(company_name)}{extension}"
    filename = f"{base}{extension}"
    owner = _owner_of_file(db, str(Path(upload_dir) / filename))
    if owner is not None and owner != company_name:
        logger.debug("file name %r is taken by %r; adding hash", filename, owner)
        filename = f"{base}_{_name_hash(company_name)}{extension}"
    return filename


def _upload_extension(filename):
    extension = os.path.splitext(filename or "")[1].lower()
    return extension or DEFAULT_EXTENSION


def _error(status, message, **extra):
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status


def _remove_stale_file(old_path, new_path, company_name):
    """Delete the previous file of a company when its new upload got another name."""
    if not old_path or old_path == new_path or not os.path.isfile(old_path):
        return
    try:
        os.remove(old_path)
        logger.info("removed previous file of %r: %s", company_name, old_path)
    except OSError as exc:
        logger.warning("could not remove previous file %s: %s", old_path, exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def upload():
    """UploadVideo: save the file, upsert the record, return its URL."""
    company_name = (request.form.get("company_name") or "").strip()
    file = request.files.get("file")
    if file is None or not file.filename:
        logger.warning("upload rejected: no file (company=%r)", company_name)
        return _error(400, "multipart field 'file' is required")
    if not company_name:
        logger.warning("upload rejected: empty company_name (file=%r)", file.filename)
        return _error(400, "form field 'company_name' is required")

    extension = _upload_extension(file.filename)
    if extension not in ALLOWED_EXTENSIONS:
        logger.warning("upload rejected: extension %r (company=%r)", extension, company_name)
        return _error(400, f"unsupported file type {extension!r}",
                      allowed=sorted(ALLOWED_EXTENSIONS))

    upload_dir = Path(current_app.config["UPLOAD_DIR"])
    db = get_db()
    previous = find_video(db, company_name)
    filename = _storage_filename(db, upload_dir, company_name, extension)
    target = upload_dir / filename

    # Write to a temporary file first so a broken upload never replaces a good video.
    temp_path = upload_dir / f".{uuid.uuid4().hex}.part"
    try:
        file.save(str(temp_path))
        os.replace(str(temp_path), str(target))
    finally:
        if temp_path.exists():
            temp_path.unlink()

    video_url = request.host_url + "media/" + quote(filename)
    upsert_video(db, company_name, str(target), video_url)
    if previous is not None:
        _remove_stale_file(previous["file_path"], str(target), company_name)

    logger.info(
        "upload %s company=%r file=%s size=%d bytes url=%s",
        "updated" if previous is not None else "created",
        company_name, filename, target.stat().st_size, video_url,
    )
    return jsonify({"company_name": company_name, "video_url": video_url})


def get_video_url():
    """GetVideoURL: return the stored URL for a company, 404 if unknown."""
    company_name = (request.args.get("company_name") or "").strip()
    if not company_name:
        logger.warning("get_video_url rejected: empty company_name")
        return _error(400, "query parameter 'company_name' is required")
    row = find_video(get_db(), company_name)
    if row is None:
        logger.warning("get_video_url: company not found %r", company_name)
        return _error(404, "company not found", company_name=company_name)
    logger.info("get_video_url company=%r -> %s", company_name, row["video_url"])
    return jsonify({"company_name": row["company_name"], "video_url": row["video_url"]})


def media(filename):
    """Serve an uploaded video (path traversal is rejected by send_from_directory)."""
    logger.debug("media request %r", filename)
    return send_from_directory(current_app.config["UPLOAD_DIR"], filename, conditional=True)


def videos():
    records = list_videos(get_db())
    logger.debug("videos: %d records", len(records))
    return jsonify({"videos": records})


def health():
    """Healthcheck: 200 when PostgreSQL answers, 503 otherwise."""
    try:
        with get_db().cursor() as cursor:
            cursor.execute("SELECT 1")
    except psycopg2.Error as exc:
        logger.error("health check failed: %s", str(exc).strip())
        return jsonify({"status": "unavailable"}), 503
    return jsonify({"status": "ok"})


def index():
    return jsonify({
        "service": "video web service",
        "endpoints": [
            "POST /upload",
            "GET /get_video_url?company_name=<name>",
            "GET /media/<filename>",
            "GET /videos",
            "GET /health",
        ],
    })


def _handle_http_error(exc):
    """JSON body for the HTTP errors Flask raises itself (404 route, 405, 413, ...)."""
    code = getattr(exc, "code", 500) or 500
    if code == 413:
        logger.warning("upload rejected: larger than %d bytes",
                       current_app.config["MAX_CONTENT_LENGTH"])
    if code >= 500:
        logger.error("server error on %s %s: %s", request.method, request.path, exc,
                     exc_info=getattr(exc, "original_exception", None))
        return _error(code, "internal server error")
    return _error(code, getattr(exc, "description", None) or str(exc))


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def _max_upload_bytes():
    megabytes = _env_number("MAX_UPLOAD_MB", DEFAULT_MAX_UPLOAD_MB, float)
    return int(megabytes * 1024 * 1024)


def configure_logging():
    """Send logs to stderr (docker compose logs) at WEB_SERVICE_LOG_LEVEL."""
    level = os.environ.get("WEB_SERVICE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def create_app(db_config=None, upload_dir=None, db_retries=None):
    """Build the Flask app; settings default to DB_* and UPLOAD_DIR environment variables.

    ``db_retries`` overrides DB_CONNECT_RETRIES for the startup wait.
    """
    app = Flask(__name__)
    app.config["DB_CONFIG"] = dict(db_config or db_config_from_env())
    upload_dir = upload_dir or os.environ.get("UPLOAD_DIR") or DEFAULT_UPLOAD_DIR
    app.config["UPLOAD_DIR"] = str(Path(upload_dir).resolve())
    app.config["MAX_CONTENT_LENGTH"] = _max_upload_bytes()
    app.json.sort_keys = False

    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
    init_db(app.config["DB_CONFIG"], retries=db_retries)

    app.teardown_appcontext(close_db)
    app.add_url_rule("/", "index", index, methods=["GET"])
    app.add_url_rule("/upload", "upload", upload, methods=["POST"])
    app.add_url_rule("/get_video_url", "get_video_url", get_video_url, methods=["GET"])
    app.add_url_rule("/media/<path:filename>", "media", media, methods=["GET"])
    app.add_url_rule("/videos", "videos", videos, methods=["GET"])
    app.add_url_rule("/health", "health", health, methods=["GET"])
    for code in JSON_ERROR_CODES:
        app.register_error_handler(code, _handle_http_error)

    logger.info("app ready: db=%s uploads=%s max_upload=%d bytes",
                _describe(app.config["DB_CONFIG"]), app.config["UPLOAD_DIR"],
                app.config["MAX_CONTENT_LENGTH"])
    return app


_wsgi_app = None
_startup_failed = False


def __getattr__(name):
    """``from web_service import app`` (the .wsgi file) builds the app once, on first use.

    Plain ``import web_service`` (tests, the dev server) never touches the database.
    If the startup wait ran out, mod_wsgi re-imports the .wsgi file on every request;
    those retries make one quick attempt instead of blocking for the whole wait again.
    """
    global _wsgi_app, _startup_failed
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if _wsgi_app is None:
        configure_logging()
        retries = 1 if _startup_failed else None
        try:
            _wsgi_app = create_app(db_retries=retries)
        except RuntimeError:
            if not _startup_failed:
                logger.error("[FIX] startup failed; next requests retry the database once each")
            _startup_failed = True
            raise
        if _startup_failed:
            logger.info("[FIX] database reachable again; app started")
            _startup_failed = False
    return _wsgi_app


def main():
    configure_logging()
    host = os.environ.get("FLASK_HOST", DEFAULT_HOST)
    raw_port = os.environ.get("FLASK_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_port)
    except ValueError:
        logger.error("invalid FLASK_PORT=%r: must be an integer", raw_port)
        return 2

    try:
        app = create_app()
    except RuntimeError as exc:
        logger.error("cannot start: %s", exc)
        return 1
    logger.info("listening on %s:%d", host, port)
    app.run(host=host, port=port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
