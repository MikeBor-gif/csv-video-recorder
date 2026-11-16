from contextlib import closing

import psycopg2
import pytest

import company_data as data
from tests import pg_helpers


@pytest.fixture
def company_db():
    config = pg_helpers.test_db_config()
    pg_helpers.reset_companies_table(config)
    data.initialize(config)
    return config


def test_migration_is_idempotent_and_pending_is_null(company_db):
    with closing(data.connect(company_db)) as db, db:
        with db.cursor() as cur:
            cur.execute("INSERT INTO companies(company_name, web) VALUES (%s,%s)",
                        ("Test GmbH", "example.de"))
        data.init_companies(db)
        row = data.list_companies(db)[0]
        assert row["status"] is None
        assert row["raw_json"] is None
        assert row["created_at"] is not None
        assert row["web"] == "example.de"
    with pytest.raises(psycopg2.IntegrityError):
        with closing(data.connect(company_db)) as db, db, db.cursor() as cur:
            cur.execute("INSERT INTO companies(company_name) VALUES (%s)", ("Test GmbH",))


def test_test_database_guard_refuses_application_database(monkeypatch):
    monkeypatch.setenv("DB_NAME", "production")
    with pytest.raises(RuntimeError):
        pg_helpers.reset_companies_table({"dbname": "production"})
