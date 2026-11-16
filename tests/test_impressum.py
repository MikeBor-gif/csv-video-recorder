from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from impressum import (FetchError, extract_visible_text, fetch_html, find_impressum_url,
                       normalize_website_url, optimize_text)


def test_visible_text_and_footer_discovery():
    text = extract_visible_text(Path("tests/fixtures/impressum/legal.html").read_text(encoding="utf-8"))
    assert "noise" not in text
    assert "Musterstraße 12" in text and "DE 123 456 789" in text
    assert find_impressum_url('<footer><a href="../legal"> ImPrInT </a></footer>',
                             'https://www.example.de/shop/') == 'https://www.example.de/legal'


def test_text_link_precedes_href_match():
    html = '<a href="/impressum-old">Legal</a><a href="/legal">Impressum</a>'
    assert find_impressum_url(html, 'https://example.de') == 'https://example.de/legal'
    assert find_impressum_url('<a href="/IMPRINT">Legal</a>', 'https://example.de') == 'https://example.de/IMPRINT'
    assert find_impressum_url('<a href="/other">Other</a>', 'https://example.de') is None


@pytest.mark.parametrize("text", ["", " a \n  b ", "a" * 8000])
def test_short_text_is_unchanged(text):
    assert optimize_text(text)[0] == text


def test_long_text_sandwich_retains_legal_block():
    legal = 'Angaben gemäß § 5 DDG\nExample GmbH\nMusterstr. 12, 10115 Berlin\nDE 123 456 789\nHRB 1234\n'
    text = 'Noise\n' * 2000 + legal + 'Unneeded legal disclaimer\n' * 2000
    result, metrics = optimize_text(text)
    assert legal in result
    assert len(result) <= 8000 and 'sandwich' in metrics['strategy']


def test_no_anchors_and_long_middle_are_bounded():
    result, metrics = optimize_text('A' * 20000)
    assert len(result) == 8000 and 'fallback' in metrics['strategy']
    text = 'Impressum\n' + 'middle ' * 3000 + '\ncontact@example.de'
    result, metrics = optimize_text(text)
    assert len(result) <= 8000 and 'contact@example.de' in result
    assert 'window' in metrics['strategy']


def test_url_normalization_and_transport(monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])
    assert normalize_website_url(' example.de ') == 'https://example.de'
    assert normalize_website_url('http://example.de') == 'http://example.de'
    assert normalize_website_url(' ') is None
    with pytest.raises(FetchError):
        normalize_website_url('ftp://example.de')
    session = Mock()
    session.get.side_effect = requests.Timeout('private request details')
    with pytest.raises(FetchError, match='Timeout') as error:
        fetch_html(session, 'https://example.de')
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('web', ['https://[broken', 'https://example.de:wrong', 'javascript:alert(1)'])
def test_malformed_website_does_not_escape_transport_classification(web):
    with pytest.raises(FetchError):
        normalize_website_url(web)


def test_malformed_impressum_candidate_does_not_hide_valid_link():
    html = '<a href="https://[broken">Impressum</a><a href="/imprint">Imprint</a>'
    assert find_impressum_url(html, 'https://example.de') == 'https://example.de/imprint'
