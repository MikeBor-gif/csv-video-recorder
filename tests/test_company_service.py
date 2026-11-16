import io
from urllib.parse import quote

import pytest

import web_service
from tests import pg_helpers


@pytest.fixture
def company_service(tmp_path):
    config = pg_helpers.test_db_config()
    pg_helpers.reset_companies_table(config)
    app = web_service.create_app(config, tmp_path / "uploads")
    return app.test_client(), config


def upload(client, content):
    return client.post("/upload_companies", data={"file": (io.BytesIO(content), "companies.csv")})


def test_browser_form_and_json_routes(company_service):
    client, config = company_service
    form = client.get("/upload_companies")
    assert form.status_code == 200
    assert b'multipart/form-data' in form.data and b'name="file"' in form.data
    assert b'href="/companies"' in form.data
    content = 'Company Name;Web\nBarolo & Champagne GbR;baroloandchampagne.com\n'.encode()
    assert upload(client, content).json == dict(imported=1, updated=0, total_rows=1)
    assert upload(client, content).json == dict(imported=0, updated=1, total_rows=1)
    rows = client.get("/companies").json
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["status"] is None
    assert client.get("/companies/" + quote("Barolo & Champagne GbR")).json == rows[0]
    assert client.get("/companies/unknown").status_code == 404
    assert client.get("/companies/" + quote("' OR 1=1 --")).status_code == 404
    assert client.get("/companies?status=0").json == []
    from contextlib import closing
    import company_data
    with closing(company_data.connect(config)) as db, db, db.cursor() as cursor:
        cursor.execute("UPDATE companies SET status=0")
    assert len(client.get("/companies?status=0").json) == 1
    assert client.get("/companies?status=1").json == []


@pytest.mark.parametrize("status", ["", "-1", "6", "no", "0.0"])
def test_invalid_filter(company_service, status):
    assert company_service[0].get("/companies?status=" + status).status_code == 400


def test_validation_does_not_partially_import(company_service):
    client, _ = company_service
    result = upload(client, b'Company Name;Web\nGood;example.de\nBad;\n')
    assert result.status_code == 400
    assert client.get("/companies").json == []
    assert client.post("/upload_companies").status_code == 400
