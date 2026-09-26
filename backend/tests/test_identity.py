import pytest

from permetheus.identity import normalize_business_id, normalize_name, normalize_website


def test_finnish_business_id_checksum_and_formats():
    assert normalize_business_id("FI", "0112038-9") == "0112038-9"
    assert normalize_business_id("FI", " 01120389 ") == "0112038-9"
    assert normalize_business_id("FI", "FI01120389") == "0112038-9"
    assert normalize_business_id("FI", "112038-9") == "0112038-9"  # legacy 6-digit form
    for bad in ("0112038-8", "abc", "12345678901"):
        with pytest.raises(ValueError):
            normalize_business_id("FI", bad)


def test_other_jurisdictions_normalize_without_checksum():
    assert normalize_business_id("SE", "556012 5790") == "5560125790"
    with pytest.raises(ValueError):
        normalize_business_id("SE", "55<script>")


def test_name_normalization_strips_trailing_legal_forms():
    assert normalize_name("Nokia Oyj") == normalize_name("NOKIA") == "nokia"
    assert normalize_name("Acme, Ltd.") == "acme"
    assert normalize_name("Oy") == "oy"  # never normalizes to empty


def test_website_normalization_and_rejections():
    assert normalize_website("WWW.Example.FI/about/") == ("https://www.example.fi/about", "example.fi")
    assert normalize_website("http://example.com")[1] == "example.com"
    for bad in ("ftp://example.com", "https://user:pw@example.com", "http://127.0.0.1", "localhost",
                "http://printer.local", "http://[::1]/"):
        with pytest.raises(ValueError):
            normalize_website(bad)
