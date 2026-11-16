from contextlib import closing

import pytest

import company_data as data
from tests.test_company_data import company_db


def test_duplicate_rows_and_reimport_preserve_enrichment(company_db):
    csv = b'Company Name;Web;City\nACME;example.de;Berlin\nACME;www.example.de;Bonn\n'
    with closing(data.connect(company_db)) as db, db:
        assert data.import_companies(db, data.parse_company_csv(csv)) == dict(
            imported=1, updated=1, total_rows=2)
        with db.cursor() as cur:
            cur.execute("UPDATE companies SET status=0, legal_name='Extracted', raw_json='{}'")
        assert data.import_companies(db, data.parse_company_csv(
            b'Company Name;Web\nACME;new.example.de\n'))["updated"] == 1
        row = data.list_companies(db)[0]
        assert (row["status"], row["legal_name"], row["city"], row["raw_json"]) == (
            data.CompanyStatus.OK, "Extracted", "Bonn", {})
        assert row["web"] == "new.example.de"


@pytest.mark.parametrize("content", [
    b'Company Name;Web\nGood;example.de\nBad;\n',
    b'Company Name;Web\nGood;example.de;extra\n', b'Company Name;Web\nBad\n',
    b'Company Name;Web;Web\nA;a;b\n', b'Company Name;Web;\nA;a;b\n',
    b'Company Name;Web\nA;\xff\n', b'Company Name;Web\nA;"unterminated',
])
def test_malformed_csv_rejected_before_import(content):
    with pytest.raises(data.CompanyCSVError):
        data.parse_company_csv(content)


def test_bom_quotes_optional_empty_and_original_web():
    rows = data.parse_company_csv('\ufeffCompany Name;Web;City\r\n"A; B"; example.de ;\r\n\r\n'.encode())
    assert rows == [dict(company_name="A; B", web=" example.de ", city="")]


def test_repository_sample_csv():
    from pathlib import Path
    rows = data.parse_company_csv(Path("companies.csv").read_bytes())
    assert [(r["company_name"], r["web"]) for r in rows] == [
        ("nutrimmun GmbH", "nutrimmun.de"), ("Barolo & Champagne GbR", "baroloandchampagne.com")]
