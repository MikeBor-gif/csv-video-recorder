"""Web service that stores recorded videos and looks them up by company name.

Run:  python web_service.py   (listens on 0.0.0.0:8000 by default)

Endpoints:
    POST /upload                         UploadVideo: multipart ``file`` + ``company_name``
    GET  /get_video_url?company_name=... GetVideoURL: 200 JSON or 404
    GET  /media/<filename>               serves an uploaded video file
    GET  /videos                         lists all records

Storage: video files live in ``uploads/`` next to this script; the SQLite
database ``videos.db`` (same folder) keeps only the company name, the absolute
file path and the public URL -- never the video bytes.

Environment variables:
    FLASK_HOST             bind address (default 0.0.0.0)
    FLASK_PORT             port (default 8000)
    WEB_SERVICE_LOG_LEVEL  DEBUG, INFO, WARNING, ERROR (default INFO)
    MAX_UPLOAD_MB          maximum upload size in megabytes (default 512)
"""

import hashlib
import logging
import mimetypes
import os
import re
import sqlite3
import sys
import unicodedata
import uuid
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from flask import Flask, current_app, g, jsonify, request, send_from_directory

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "videos.db"
DEFAULT_UPLOAD_DIR = BASE_DIR / "uploads"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_MAX_UPLOAD_MB = 512

ALLOWED_EXTENSIONS = {".webm", ".mp4", ".ogg", ".mov"}
DEFAULT_EXTENSION = ".webm"

# Status codes answered with a JSON body instead of Flask's HTML error page.
JSON_ERROR_CODES = (400, 404, 405, 413, 500)

_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# SQLite waits this long for a competing writer before "database is locked".
DB_TIMEOUT_SECONDS = 10

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT    NOT NULL UNIQUE,
    file_path    TEXT    NOT NULL,  -- absolute path to the file on disk
    video_url    TEXT    NOT NULL,  -- public URL to the video
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

# Windows may not know the .webm type; browsers need it to play the file inline.
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/ogg", ".ogg")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/quicktime", ".mov")

logger = logging.getLogger("web_service")

# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------


def _connect(db_path):
    connection = sqlite3.connect(str(db_path), timeout=DB_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {DB_TIMEOUT_SECONDS * 1000}")
    return connection


def init_db(db_path):
    """Create the database file and the ``videos`` table if they do not exist."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(db_path)
    try:
        connection.executescript(SCHEMA_SQL)
        # WAL lets readers work while one upload writes; the mode is stored in the file.
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        connection.commit()
    finally:
        connection.close()
    logger.info("DB initialised at %s (journal_mode=%s)", db_path, mode)


def get_db():
    """Return the SQLite connection of the current request (one per request/thread)."""
    if "db" not in g:
        g.db = _connect(current_app.config["DB_PATH"])
        logger.debug("DB connection opened")
    return g.db


def close_db(_exc=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()
        logger.debug("DB connection closed")


def upsert_video(db, company_name, file_path, video_url):
    """Insert a record or update the existing one for ``company_name``."""
    db.execute(
        """
        INSERT INTO videos (company_name, file_path, video_url)
        VALUES (?, ?, ?)
        ON CONFLICT(company_name) DO UPDATE SET
            file_path  = excluded.file_path,
            video_url  = excluded.video_url,
            updated_at = datetime('now')
        """,
        (company_name, file_path, video_url),
    )
    db.commit()
    logger.debug("DB upsert company=%r file_path=%r", company_name, file_path)


def find_video(db: sqlite3.Connection, company_name: str) -> Optional[sqlite3.Row]:
    row = db.execute(
        "SELECT * FROM videos WHERE company_name = ?", (company_name,)
    ).fetchone()
    logger.debug("DB find company=%r found=%s", company_name, row is not None)
    return row


def list_videos(db: sqlite3.Connection) -> List[dict]:
    rows = db.execute(
        "SELECT company_name, file_path, video_url, created_at, updated_at "
        "FROM videos ORDER BY updated_at DESC, id DESC"
    ).fetchall()
    return [dict(row) for row in rows]


def _owner_of_file(db: sqlite3.Connection, file_path: str) -> Optional[str]:
    # NOCASE: on Windows/macOS "ACME.webm" and "acme.webm" are the same file.
    row = db.execute(
        "SELECT company_name FROM videos WHERE file_path = ? COLLATE NOCASE", (file_path,)
    ).fetchone()
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
    raw = os.environ.get("MAX_UPLOAD_MB", str(DEFAULT_MAX_UPLOAD_MB))
    try:
        megabytes = float(raw)
    except ValueError:
        logger.warning("invalid MAX_UPLOAD_MB=%r; using %d", raw, DEFAULT_MAX_UPLOAD_MB)
        megabytes = DEFAULT_MAX_UPLOAD_MB
    return int(megabytes * 1024 * 1024)


def create_app(db_path=None, upload_dir=None):
    """Build the Flask app; paths default to videos.db and uploads/ next to this file."""
    app = Flask(__name__)
    app.config["DB_PATH"] = str(Path(db_path or DEFAULT_DB_PATH).resolve())
    app.config["UPLOAD_DIR"] = str(Path(upload_dir or DEFAULT_UPLOAD_DIR).resolve())
    app.config["MAX_CONTENT_LENGTH"] = _max_upload_bytes()
    app.json.sort_keys = False

    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
    init_db(app.config["DB_PATH"])

    app.teardown_appcontext(close_db)
    app.add_url_rule("/upload", "upload", upload, methods=["POST"])
    app.add_url_rule("/get_video_url", "get_video_url", get_video_url, methods=["GET"])
    app.add_url_rule("/media/<path:filename>", "media", media, methods=["GET"])
    app.add_url_rule("/videos", "videos", videos, methods=["GET"])
    for code in JSON_ERROR_CODES:
        app.register_error_handler(code, _handle_http_error)

    logger.info("app ready: db=%s uploads=%s max_upload=%d bytes",
                app.config["DB_PATH"], app.config["UPLOAD_DIR"],
                app.config["MAX_CONTENT_LENGTH"])
    return app


def main():
    level = os.environ.get("WEB_SERVICE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("FLASK_HOST", DEFAULT_HOST)
    raw_port = os.environ.get("FLASK_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_port)
    except ValueError:
        logger.error("invalid FLASK_PORT=%r: must be an integer", raw_port)
        return 2

    app = create_app()
    logger.info("listening on %s:%d", host, port)
    app.run(host=host, port=port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
