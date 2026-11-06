"""PostgreSQL helpers for the tests: a separate test database, reset between tests.

The tests need a running PostgreSQL and the DB_* environment variables. The
easiest way is to run them inside the web container:

    docker compose exec -T web python -m unittest discover -s tests -v

The test database is TEST_DB_NAME (default: "<DB_NAME>_test"), so the real
"videos" table is never touched. Without PostgreSQL the tests are skipped.
"""

import os
import unittest

import psycopg2
from psycopg2 import sql

import web_service

SKIP_MESSAGE = ("PostgreSQL is not available ({}); run the tests inside the container: "
                "docker compose exec -T web python -m unittest discover -s tests -v")

_database_ready = False


def test_db_config() -> dict:
    """Connection settings of the test database; raises SkipTest without PostgreSQL."""
    try:
        config = web_service.db_config_from_env()
    except RuntimeError as exc:
        raise unittest.SkipTest(SKIP_MESSAGE.format(exc))
    config["dbname"] = os.environ.get("TEST_DB_NAME") or f"{config['dbname']}_test"
    _ensure_database(config)
    return config


def _ensure_database(config):
    global _database_ready
    if _database_ready:
        return
    try:
        admin = psycopg2.connect(**dict(config, dbname="postgres"), connect_timeout=3)
    except psycopg2.OperationalError as exc:
        raise unittest.SkipTest(SKIP_MESSAGE.format(str(exc).strip()))
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (config["dbname"],))
            if cursor.fetchone() is None:
                cursor.execute(sql.SQL("CREATE DATABASE {}").format(
                    sql.Identifier(config["dbname"])))
    finally:
        admin.close()
    _database_ready = True


def reset_videos_table(config):
    """Drop the table so every test starts from an empty database."""
    connection = psycopg2.connect(**config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS videos")
        connection.commit()
    finally:
        connection.close()


def query(config, sql_text, params=()):
    """Run a SELECT in the test database and return rows as tuples."""
    connection = psycopg2.connect(**config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql_text, params)
            return cursor.fetchall()
    finally:
        connection.close()
