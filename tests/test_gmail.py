"""Gmail connection: read-only scope, signed state, nothing stored before consent."""
from urllib.parse import parse_qs, urlparse

import pytest

from app import gmail


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("PAYDESK_SECRET", "s")
    monkeypatch.setattr(gmail, "TOKEN_FILE", str(tmp_path / "tok.json"))


def test_consent_url_is_read_only_and_signed():
    q = parse_qs(urlparse(gmail.auth_url("http://localhost:8000/")).query)
    assert q["scope"][0].endswith("gmail.readonly") and "gmail.send" not in q["scope"][0]
    assert q["redirect_uri"][0] == "http://localhost:8000/gmail/callback"
    nonce, sig = q["state"][0].split(".")
    assert sig == gmail._state_sig(nonce)


def test_forged_state_is_refused_and_nothing_is_stored():
    with pytest.raises(ValueError):
        gmail.exchange("code", "abc.forged", "http://localhost:8000/")
    assert gmail.status() == {"configured": True, "connected": False, "email": None, "query": gmail.query()}
