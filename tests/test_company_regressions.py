"""Regression checks for the Task 3 review; all HTTP/LLM responses are local fakes."""

import json
import socket
import time
import multiprocessing
from unittest.mock import Mock, MagicMock, patch
from urllib.parse import quote

import pytest

import impressum
import enrich_companies
from company_data import CompanyStatus
from tests.test_company_service import company_service, upload
from tests.test_company_data import company_db


def page(body=b"<h1>Impressum</h1>", status=200, headers=None):
    response = Mock()
    response.status_code = status
    response.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
    response.content = body
    response.text = body.decode("utf-8")
    response.url = "https://public.example/"
    response.iter_content.return_value = iter([body])
    return response


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://10.0.0.1/", "http://169.254.169.254/",
                                 "http://[::1]/", "http://[::ffff:127.0.0.1]/"])
def test_private_destination_not_requested(url):
    session = Mock()
    session.get.return_value = page()
    with pytest.raises(impressum.FetchError):
        impressum.fetch_html(session, url)
    session.get.assert_not_called()


def test_private_dns_answer_not_requested(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80))])
    session = Mock()
    session.get.return_value = page()
    with pytest.raises(impressum.FetchError):
        impressum.fetch_html(session, "http://private.example/")
    session.get.assert_not_called()


def test_redirect_to_private_destination_blocked(public_dns):
    session = Mock()
    response = page(status=302, headers={"Location": "http://127.0.0.1/private"})
    session.get.return_value = response
    with pytest.raises(impressum.FetchError):
        impressum.fetch_html(session, "https://public.example/")
    assert session.get.call_count == 1
    response.close.assert_called()


def test_large_decoded_body_is_rejected(public_dns):
    session = Mock()
    response = page(b"x" * (3 * 1024 * 1024), headers={
        'Content-Type': 'text/html', 'Content-Encoding': 'gzip', 'Content-Length': '100'})
    session.get.return_value = response
    with pytest.raises(impressum.FetchError):
        impressum.fetch_html(session, "https://public.example/")
    response.close.assert_called()


def test_overall_deadline_applies_to_continuous_chunks(public_dns, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(impressum.time, "monotonic", lambda: now[0])
    response = page()
    def chunks(**kwargs):
        now[0] = 100.0
        yield b"still sending data"
    response.iter_content.side_effect = chunks
    session = Mock()
    session.get.return_value = response
    with pytest.raises(impressum.FetchError):
        impressum.fetch_html(session, "https://public.example/")
    response.close.assert_called()


@pytest.mark.parametrize("raw", [
    {"company_name": "A\x00B"}, {"postal_address": {"city": "A\ud800B"}},
    {"extra": ["A\x00B"]}, {"extra": {"A\udfffB": "value"}}, {"extra": 1e999},
])
def test_unstorable_completion_becomes_parsing_failed(monkeypatch, raw):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-regression-key")
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {
        "choices": [{"message": {"content": json.dumps(raw)}}]}
    result = impressum.extract_company_data("visible text", "CSV", session, Mock())
    assert result["status"] == CompanyStatus.PARSING_FAILED
    assert result["raw_json"] == {}
    assert session.post.call_count == 2


def test_nul_completion_does_not_abort_following_company(company_db, monkeypatch):
    import company_data
    from contextlib import closing
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-regression-key")
    with closing(company_data.connect(company_db)) as db, db:
        company_data.import_companies(db, [dict(company_name="First", web="public.example"),
                                          dict(company_name="Second", web="public.example")])
    api = Mock()
    api.post.return_value.status_code = 200
    api.post.return_value.json.return_value = {"choices":[{"message":{"content":'{"company_name":"A\\u0000B"}'}}]}
    def extractor(text, name):
        if name == "First":
            return impressum.extract_company_data(text, name, api, Mock())
        return impressum.extraction_result({"postal_address": dict(street="S", house_no="1",
            postcode="12345", city="C")}, name)
    with patch.object(enrich_companies, "fetch_html", return_value=(
            '<h1>Impressum</h1><a href="/">Impressum</a>', 'https://public.example/')), \
            patch.object(enrich_companies, "extract_company_data", side_effect=extractor):
        result = enrich_companies.run(company_db, session=Mock(), sleep=Mock())
    assert result["saved"] == 2
    with closing(company_data.connect(company_db)) as db:
        assert [r['status'] for r in company_data.list_companies(db)] == [CompanyStatus.PARSING_FAILED, CompanyStatus.OK]


def test_company_name_with_slash_round_trips(company_service):
    client, _ = company_service
    name = "A/B & C GmbH"
    assert upload(client, ("Company Name;Web\n" + name + ";example.de\n").encode()).status_code == 200
    response = client.get("/companies/" + quote(name, safe=""))
    assert response.status_code == 200
    assert response.json["company_name"] == name


def _hanging_fetch_worker(connection, url):
    time.sleep(30)


def test_hard_deadline_terminates_stuck_network_worker(monkeypatch):
    import requests
    monkeypatch.setattr(impressum, "_fetch_worker", _hanging_fetch_worker)
    monkeypatch.setattr(impressum, "FETCH_TOTAL_TIMEOUT", 0.2)
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    with requests.Session() as session, pytest.raises(impressum.FetchError, match="deadline"):
        impressum.fetch_html(session, "https://public.example/")
    assert time.monotonic() - started < 3
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_dns_is_pinned_with_original_host_and_tls_checks(public_dns):
    session = Mock()
    response = page()
    response.encoding = 'utf-8'
    session.get.return_value = response
    text, url = impressum.fetch_html(session, 'https://public.example/legal')
    assert url == 'https://public.example/legal' and 'Impressum' in text
    assert session.get.call_args.args[0] == 'https://93.184.216.34:443/legal'
    options = session.get.call_args.kwargs
    assert options['headers']['Host'] == 'public.example'
    assert options['verify'] is True and options['allow_redirects'] is False
    assert options['stream'] is True and session.trust_env is False
    adapter = session.mount.call_args_list[0].args[1]
    assert adapter.poolmanager.connection_pool_kw['assert_hostname'] == 'public.example'
    assert adapter.poolmanager.connection_pool_kw['server_hostname'] == 'public.example'


def test_mixed_public_private_dns_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))
        for address in ('93.184.216.34', '10.0.0.1')])
    session = Mock()
    with pytest.raises(impressum.FetchError):
        impressum.fetch_html(session, 'https://mixed.example/')
    session.get.assert_not_called()


def test_live_verifier_fails_when_cli_fails(monkeypatch):
    from scripts import verify_companies as verify
    row = {key: None for key in verify.EXTRACTED}
    row.update(id=1, company_name='Sample', status=0, impressum_url='https://public.example/impressum')
    session = MagicMock()
    session.__enter__.return_value = session
    def get(url, **kwargs):
        response = Mock()
        response.text = '<form enctype="multipart/form-data"><input name="file"></form>'
        response.json.return_value = [row] if url.endswith('/companies') else row
        return response
    session.get.side_effect = get
    session.post.return_value.json.return_value = dict(imported=0, updated=2, total_rows=2)
    monkeypatch.setattr(verify.requests, 'Session', lambda: session)
    monkeypatch.setattr(verify.subprocess, 'run', lambda *a, **kw: verify.subprocess.CompletedProcess(a[0], 1))
    assert verify.main(['--live']) == 1


def test_public_redirect_keeps_logical_url_and_checks_each_dns_answer(monkeypatch):
    resolver = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])
    monkeypatch.setattr(socket, 'getaddrinfo', resolver)
    redirect = page(status=302, headers={'Location':'https://other.example/impressum'})
    final = page()
    final.encoding = 'utf-8'
    session = Mock()
    session.get.side_effect = [redirect, final]
    text, url = impressum.fetch_html(session, 'https://public.example/')
    assert url == 'https://other.example/impressum'
    assert 'Impressum' in text and resolver.call_count == 2
    assert session.get.call_args_list[1].kwargs['headers']['Host'] == 'other.example'
    redirect.close.assert_called_once()
    final.close.assert_called_once()


def test_bad_json_number_from_exponent_is_rejected(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fake-regression-key')
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {'choices':[{'message':{'content':'{"extra":1e999}'}}]}
    assert impressum.extract_company_data('text','CSV',session,Mock())['raw_json'] == {}


def test_valid_unicode_is_preserved():
    raw = {'company_name':'Münster GmbH 🌍', 'extra':{'unicode':'😀'}}
    assert impressum.extraction_result(raw, 'CSV')['raw_json'] == raw
