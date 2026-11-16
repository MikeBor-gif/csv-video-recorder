"""Company storage shared by Flask and the standalone enrichment command."""

import logging
import os
import csv
import io
from contextlib import closing
from enum import IntEnum

import psycopg2
from psycopg2.extras import RealDictCursor, Json
from psycopg2 import sql

logger = logging.getLogger(__name__)

CSV_FIELDS = {
    "Company Name": "company_name", "Language": "language", "Country": "country",
    "Postcode": "postcode", "City": "city", "Street": "street", "HouseNo": "house_no",
    "Web": "web",
}


class CompanyCSVError(ValueError):
    """An invalid CSV upload, safe to display to the user."""


class CompanyStatus(IntEnum):
    OK = 0
    NO_IMPRESSUM_LINK_FOUND = 1
    PARSING_FAILED = 2
    ADDRESS_INCOMPLETE = 3
    ERROR = 4
    NO_WEBSITE = 5


COMPANIES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    id SERIAL PRIMARY KEY,
    company_name TEXT NOT NULL UNIQUE,
    language TEXT, country TEXT, postcode TEXT, city TEXT, street TEXT,
    house_no TEXT, web TEXT,
    status INTEGER,
    impressum_url TEXT, legal_name TEXT, postal_address TEXT,
    handelsregister TEXT, vat_number TEXT, phone TEXT, email TEXT,
    responsible_person TEXT, raw_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def db_config_from_env():
    required = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("missing environment variable(s): " + ", ".join(missing))
    try:
        port = int(os.environ.get("DB_PORT") or "5432")
    except ValueError:
        raise RuntimeError("invalid DB_PORT: must be an integer") from None
    return dict(host=os.environ["DB_HOST"], port=port,
                dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                password=os.environ["DB_PASSWORD"])


def connect(config):
    logger.debug("opening company database connection")
    return psycopg2.connect(**config, connect_timeout=5, cursor_factory=RealDictCursor)


def init_companies(db):
    """Execute an idempotent migration in the caller's transaction."""
    logger.debug("initializing companies schema")
    with db.cursor() as cursor:
        cursor.execute(COMPANIES_SCHEMA_SQL)
    logger.info("companies schema ready")


def initialize(config):
    with closing(connect(config)) as db, db:
        init_companies(db)


def list_companies(db, status=None, company_name=None):
    clauses, params = [], []
    if status is not None:
        clauses.append("status = %s")
        params.append(int(status))
    if company_name is not None:
        clauses.append("company_name = %s")
        params.append(company_name)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db.cursor() as cursor:
        cursor.execute("SELECT * FROM companies" + where + " ORDER BY id", params)
        rows = [dict(row) for row in cursor.fetchall()]
    logger.debug("company read rows=%d status=%s", len(rows), status)
    return rows


def serialize_company(row):
    result = dict(row)
    for key in ("created_at", "updated_at"):
        if result.get(key) is not None:
            result[key] = result[key].isoformat()
    return result


def parse_company_csv(content):
    """Validate the entire file before any database mutation."""
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=";",
                                quotechar='"', strict=True)
        headers = reader.fieldnames or []
        if (not headers or any(not h for h in headers)
                or len(set(headers)) != len(headers)
                or not {"Company Name", "Web"}.issubset(headers)):
            raise CompanyCSVError("CSV needs unique headers including Company Name and Web")
        if set(headers) - (set(CSV_FIELDS) | {"URL"}):
            raise CompanyCSVError("CSV has unsupported columns")
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise CompanyCSVError("wrong column count at line %d" % reader.line_num)
            if any("\x00" in value for value in row.values()):
                raise CompanyCSVError("NUL character at line %d" % reader.line_num)
            if not row["Company Name"].strip() or not row["Web"].strip():
                raise CompanyCSVError("Company Name and Web required at line %d" % reader.line_num)
            rows.append({column: row[header] for header, column in CSV_FIELDS.items()
                         if header in row})
        logger.debug("CSV validated rows=%d columns=%d", len(rows), len(headers))
        return rows
    except (UnicodeError, csv.Error) as exc:
        raise CompanyCSVError("invalid UTF-8 CSV (%s)" % type(exc).__name__) from None


def import_companies(db, rows):
    """Upsert CSV columns only; caller owns commit/rollback."""
    counts = dict(imported=0, updated=0, total_rows=len(rows))
    with db.cursor() as cursor:
        # Serialize import counters without relying on PostgreSQL tuple internals.
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (73823003,))
        for number, row in enumerate(rows, 1):
            cursor.execute("SELECT id FROM companies WHERE company_name=%s", (row["company_name"],))
            existed = cursor.fetchone() is not None
            columns = list(row)
            updates = [sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
                       for c in columns if c != "company_name"]
            updates.append(sql.SQL("updated_at = clock_timestamp()"))
            statement = sql.SQL("INSERT INTO companies ({}) VALUES ({}) "
                                "ON CONFLICT (company_name) DO UPDATE SET {}").format(
                sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                sql.SQL(", ").join(updates))
            cursor.execute(statement, [row[c] for c in columns])
            counts["updated" if existed else "imported"] += 1
            logger.debug("CSV row=%d operation=%s", number, "update" if existed else "insert")
    return counts


def select_for_enrichment(config, company_name=None, status=None, limit=None):
    clauses, params = [], []
    if status is None:
        clauses.append("(status IS NULL OR status <> %s)")
        params.append(int(CompanyStatus.OK))
    else:
        clauses.append("status = %s")
        params.append(int(status))
    if company_name is not None:
        clauses.append("company_name = %s")
        params.append(company_name)
    statement = "SELECT * FROM companies WHERE " + " AND ".join(clauses) + " ORDER BY id"
    if limit is not None:
        statement += " LIMIT %s"
        params.append(limit)
    with closing(connect(config)) as db, db, db.cursor() as cursor:
        cursor.execute(statement, params)
        rows = [dict(row) for row in cursor.fetchall()]
    logger.debug("enrichment selection rows=%d status=%s limit=%s", len(rows), status, limit)
    return rows


def save_enrichment(config, snapshot, changes):
    allowed = {"status", "impressum_url", "legal_name", "postal_address", "handelsregister",
               "vat_number", "phone", "email", "responsible_person", "raw_json"}
    if not changes or set(changes) - allowed:
        raise ValueError("unsupported enrichment update")
    columns = list(changes)
    values = [Json(changes[c]) if c == "raw_json" else changes[c] for c in columns]
    setters = [sql.SQL("{} = %s").format(sql.Identifier(c)) for c in columns]
    setters.append(sql.SQL("updated_at = clock_timestamp()"))
    statement = sql.SQL("UPDATE companies SET {} WHERE id=%s AND updated_at=%s").format(
        sql.SQL(", ").join(setters))
    with closing(connect(config)) as db, db, db.cursor() as cursor:
        cursor.execute(statement, values + [snapshot["id"], snapshot["updated_at"]])
        saved = cursor.rowcount == 1
    if saved:
        logger.debug("enrichment saved id=%s status=%s", snapshot["id"], changes.get("status"))
    else:
        logger.warning("enrichment snapshot changed; skipping id=%s", snapshot["id"])
    return saved
