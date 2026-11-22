"""Connect a Google account and pull payment mail into the desk.

- OAuth 2.0 authorization-code flow, scope gmail.readonly (the desk can read, never send or delete).
- Needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (a Web OAuth client with redirect URI
  http://localhost:8000/gmail/callback). The refresh token is stored in data/gmail_token.json
  (git-ignored, owner-only); disconnecting revokes it at Google and deletes the file.
- Only messages matching the payment query are listed (invoices, bills, payments, remittance,
  bank-detail changes, purchase orders). A person chooses which ones to read into the desk.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

from . import auth

TOKEN_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "gmail_token.json")
SCOPES = "openid email https://www.googleapis.com/auth/gmail.readonly"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
DEFAULT_QUERY = ('newer_than:90d (invoice OR "tax invoice" OR bill OR payment OR remittance OR "bank details" '
                 'OR "purchase order" OR GSTIN OR "payment advice")')


class NotConnected(Exception):
    pass


def client_id():
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def client_secret():
    return os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def configured() -> bool:
    return bool(client_id() and client_secret())


def redirect_uri(base: str) -> str:
    return base.rstrip("/") + "/gmail/callback"


def _state_sig(nonce: str) -> str:
    return hmac.new(auth._secret(), ("gmail:" + nonce).encode(), hashlib.sha256).hexdigest()[:32]


def auth_url(base: str) -> str:
    nonce = secrets.token_urlsafe(12)
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": client_id(), "redirect_uri": redirect_uri(base), "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
        "state": f"{nonce}.{_state_sig(nonce)}"})


def exchange(code: str, state: str, base: str) -> dict:
    nonce, _, sig = (state or "").partition(".")
    if not nonce or not hmac.compare_digest(sig, _state_sig(nonce)):
        raise ValueError("The sign-in response did not come from this desk (state mismatch).")
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=20, data={
        "code": code, "client_id": client_id(), "client_secret": client_secret(),
        "redirect_uri": redirect_uri(base), "grant_type": "authorization_code"})
    if r.status_code != 200:
        raise ValueError(f"Google refused the code ({r.status_code}).")
    tok = r.json()
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    info = httpx.get(f"{API}/profile", headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=20)
    tok["email"] = info.json().get("emailAddress") if info.status_code == 200 else None
    _save(tok)
    return {"email": tok["email"]}


def _save(tok):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(tok, f)
    os.chmod(TOKEN_FILE, 0o600)


def _load():
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def status() -> dict:
    tok = _load()
    return {"configured": configured(), "connected": bool(tok), "email": (tok or {}).get("email"), "query": query()}


def query() -> str:
    from . import config
    return config.all_rules().get("gmail_query", DEFAULT_QUERY)


def _access_token() -> str:
    tok = _load()
    if not tok:
        raise NotConnected("Connect a Google account first.")
    if tok.get("expires_at", 0) > time.time():
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise NotConnected("The Google connection expired. Connect again.")
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=20, data={
        "client_id": client_id(), "client_secret": client_secret(),
        "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise NotConnected("Google refused to refresh the connection. Connect again.")
    new = r.json()
    tok.update(access_token=new["access_token"], expires_at=time.time() + int(new.get("expires_in", 3600)) - 60)
    _save(tok)
    return tok["access_token"]


def _get(path, **params):
    r = httpx.get(API + path, params=params, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=30)
    if r.status_code == 401:
        raise NotConnected("Google rejected the connection. Connect again.")
    r.raise_for_status()
    return r.json()


def list_payment_mail(limit=25) -> list:
    ids = _get("/messages", q=query(), maxResults=limit).get("messages", [])
    out = []
    for m in ids:
        d = _get(f"/messages/{m['id']}", format="metadata", metadataHeaders=["From", "Subject", "Date"])
        h = {x["name"]: x["value"] for x in d.get("payload", {}).get("headers", [])}
        parts = d.get("payload", {}).get("parts", []) or []
        out.append({"id": m["id"], "from": h.get("From"), "subject": h.get("Subject"), "date": h.get("Date"),
                    "snippet": d.get("snippet"), "attachments": sum(1 for p in parts if p.get("filename"))})
    return out


def raw_message(msg_id: str) -> bytes:
    d = _get(f"/messages/{msg_id}", format="raw")
    return base64.urlsafe_b64decode(d["raw"] + "=" * (-len(d["raw"]) % 4))


def disconnect():
    tok = _load()
    if tok:
        try:
            httpx.post("https://oauth2.googleapis.com/revoke", params={"token": tok.get("refresh_token") or tok.get("access_token")}, timeout=15)
        except httpx.HTTPError:
            pass
        os.remove(TOKEN_FILE)
