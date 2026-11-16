import json
from unittest.mock import Mock

import pytest
import requests

from company_data import CompanyStatus as Status
from impressum import DeepSeekError, EnrichmentConfigError, extract_company_data, extraction_result


def complete():
    return dict(company_name="Name GmbH", postal_address=dict(street="Street", house_no="12a",
                postcode="01234", city="Town", country=None), email="MAIL@EXAMPLE.DE")


def response(content, status=200):
    result = Mock(status_code=status)
    result.json.return_value = dict(choices=[dict(message=dict(content=content))], usage={"total_tokens": 33})
    return result


@pytest.mark.parametrize("missing", [None, 'street', 'house_no', 'postcode', 'city'])
def test_address_completeness_and_optional_fields(missing):
    raw = complete()
    if missing:
        raw["postal_address"][missing] = " "
    result = extraction_result(raw, "CSV name")
    assert result["status"] == (Status.ADDRESS_INCOMPLETE if missing else Status.OK)
    assert result["email"] == 'mail@example.de'
    assert result["raw_json"] == raw


@pytest.mark.parametrize("raw", [{}, {"postal_address": {}}, {"company_name": " ", "phone": None},
                                     {"postal_address": dict.fromkeys(['street','house_no','postcode','city'])}])
def test_empty_fields_precede_csv_fallback(raw):
    result = extraction_result(raw, 'CSV')
    assert result['status'] == Status.PARSING_FAILED and result['legal_name'] == 'CSV'


@pytest.mark.parametrize('email', ['[email protected]', '[email\u00a0protected]', 'not-an-email'])
def test_masked_email_is_not_presented_as_an_actual_address(email):
    raw = complete()
    raw['email'] = email
    result = extraction_result(raw, 'CSV')
    assert result['email'] is None
    assert result['status'] == Status.OK
    assert result['raw_json'] == raw


def test_one_request_and_exact_request_contract(monkeypatch, caplog):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret-marker')
    session = Mock()
    session.post.return_value = response(json.dumps(complete()))
    with caplog.at_level('DEBUG'):
        assert extract_company_data('visible text', 'CSV', session)['status'] == Status.OK
    assert session.post.call_count == 1
    kwargs = session.post.call_args.kwargs
    assert kwargs['json']['response_format'] == {'type': 'json_object'}
    assert kwargs['json']['max_tokens'] == 1024
    assert kwargs['json']['temperature'] == 0
    assert kwargs['timeout'] == 60
    assert kwargs['headers']['Authorization'] == 'Bearer test-secret-marker'
    assert 'test-secret-marker' not in caplog.text


@pytest.mark.parametrize('content', [' ', 'not json', '[]', '{', '{"postal_address":"invalid"}',
                                    '{"phone":123}', '{"unexpected":NaN}', None])
def test_invalid_output_bounded_retries(monkeypatch, content):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fake')
    session, sleep = Mock(), Mock()
    session.post.return_value = response(content)
    assert extract_company_data('text', 'CSV', session, sleep)['status'] == Status.PARSING_FAILED
    assert session.post.call_count == 2 and sleep.call_count == 1


@pytest.mark.parametrize('status, attempts', [(400,1), (401,1), (402,1), (403,1), (429,2), (500,2)])
def test_http_failure_bounded(monkeypatch, status, attempts):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fake')
    session = Mock()
    session.post.return_value = response('', status)
    with pytest.raises(DeepSeekError):
        extract_company_data('text', 'CSV', session, Mock())
    assert session.post.call_count == attempts


def test_transport_and_config_failure(monkeypatch, caplog):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    session = Mock()
    with pytest.raises(EnrichmentConfigError):
        extract_company_data('text', 'CSV', session)
    session.post.assert_not_called()
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fake')
    session.post.side_effect = requests.Timeout('sensitive details')
    with pytest.raises(DeepSeekError):
        extract_company_data('text', 'CSV', session, Mock())
    assert session.post.call_count == 2 and 'sensitive details' not in caplog.text
