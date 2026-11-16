from contextlib import closing
from unittest.mock import Mock, patch

import pytest

import company_data as data
import enrich_companies as enrich
from impressum import DeepSeekError, FetchError, extraction_result
from tests.test_company_data import company_db


def seed(config):
    with closing(data.connect(config)) as db, db:
        for i, status in enumerate([None] + list(data.CompanyStatus)):
            with db.cursor() as cur:
                cur.execute("INSERT INTO companies(company_name,web,status) VALUES (%s,%s,%s)",
                            ("Company %d" % i, "example.de", status))


def test_selection_combinations(company_db):
    seed(company_db)
    assert len(data.select_for_enrichment(company_db)) == 6
    assert [r['id'] for r in data.select_for_enrichment(company_db, limit=2)] == [1, 3]
    assert data.select_for_enrichment(company_db, 'Company 1') == []
    assert len(data.select_for_enrichment(company_db, 'Company 1', data.CompanyStatus.OK)) == 1


def test_snapshot_conflict_and_per_attempt_fields(company_db):
    seed(company_db)
    row = data.select_for_enrichment(company_db, limit=1)[0]
    result = extraction_result({'phone':'123'}, row['company_name'])
    assert data.save_enrichment(company_db, row, result)
    assert not data.save_enrichment(company_db, row, {'status': data.CompanyStatus.ERROR})
    fresh = data.select_for_enrichment(company_db, limit=1)[0]
    assert fresh['phone'] == '123' and fresh['raw_json'] == {'phone':'123'}
    assert data.save_enrichment(company_db, fresh, {'status': data.CompanyStatus.ERROR})
    fresh = data.select_for_enrichment(company_db, limit=1)[0]
    assert fresh['phone'] == '123'


def test_empty_web_and_no_link_skip_api():
    row = dict(id=1, company_name='Test', web=' ')
    session, extractor = Mock(), Mock()
    assert enrich.enrich_one(row, session, extractor)['status'] == data.CompanyStatus.NO_WEBSITE
    row['web'] = 'example.de'
    with patch.object(enrich, 'fetch_html', return_value=('<a href="/about">About</a>', 'https://example.de')):
        assert enrich.enrich_one(row, session, extractor)['status'] == data.CompanyStatus.NO_IMPRESSUM_LINK_FOUND
    extractor.assert_not_called()


def test_found_link_transport_and_api_failures():
    row = dict(id=1, company_name='Test', web='example.de')
    home = ('<a href="/impressum">Impressum</a>', 'https://example.de')
    with patch.object(enrich, 'fetch_html', side_effect=[home, FetchError('HTTP 500')]):
        result = enrich.enrich_one(row, Mock())
    assert result == dict(status=data.CompanyStatus.ERROR, impressum_url='https://example.de/impressum')
    with patch.object(enrich, 'fetch_html', side_effect=[home, ('<h1>Impressum</h1>', 'https://example.de/impressum')]):
        result = enrich.enrich_one(row, Mock(), Mock(side_effect=DeepSeekError('HTTP 401')))
    assert result['status'] == data.CompanyStatus.ERROR


def test_batch_continues_and_default_repeat_skips_ok(company_db):
    seed(company_db)
    with patch.object(enrich, 'enrich_one', side_effect=[{'status': data.CompanyStatus.ERROR},
            extraction_result({'postal_address':dict(street='S',house_no='1',postcode='12345',city='C')},'C')]):
        counts = enrich.run(company_db, limit=2, session=Mock(), sleep=Mock())
    assert counts['saved'] == 2 and counts['failed'] == 1
    with patch.object(enrich, 'enrich_one') as action:
        assert enrich.run(company_db, company_name='Company 2', session=Mock())['selected'] == 0
        action.assert_not_called()


@pytest.mark.parametrize('args', [['--limit','0'],['--limit','-1'],['--status','6']])
def test_invalid_cli_args(args):
    with pytest.raises(SystemExit):
        enrich.main(args)
