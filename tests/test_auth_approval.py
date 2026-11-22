"""Sign-in gate, signed sessions, and two-person approval."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import auth, db, pipeline
from app.db import connect
from tests.test_pipeline import POS, VENDORS, FakeCD, FakePay, inv


@pytest.fixture(autouse=True)
def env(monkeypatch):
    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    monkeypatch.setenv("PAYDESK_SECRET", "test-secret")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    yield


def test_session_is_signed_and_tamper_proof():
    c = auth.make_session({"name": "A", "email": "a@x.test", "provider": "local"})
    assert auth.read_session(c)["email"] == "a@x.test"
    body, sig = c.rsplit(".", 1)
    assert auth.read_session(body + "." + "0" * len(sig)) is None


def test_pages_and_api_need_sign_in_and_approver_is_the_session_user():
    from app.server import app
    client = TestClient(app)
    assert client.get("/", follow_redirects=False).status_code in (302, 307)
    assert client.get("/state").status_code == 401
    assert client.get("/signin").status_code == 200
    r = client.post("/auth/local", json={"name": "Priya", "email": "priya@company.test"})
    assert r.status_code == 200
    assert client.get("/me").json()["email"] == "priya@company.test"
    assert client.get("/state").status_code == 200


def test_local_signup_refused_when_google_is_configured(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "abc.apps.googleusercontent.com")
    from app.server import app
    assert TestClient(app).post("/auth/local", json={"name": "X", "email": "x@y.test"}).status_code == 403


def test_large_payment_needs_two_different_approvers():
    pay = FakePay()
    conn = connect()
    big = [{"po_number": "PO-1", "amount": 250000}]
    rid = pipeline.submit({"invoices": [inv("BIG-1", "₹2,50,000")], "purchase_orders": big}, conn)
    s = pipeline.process(rid, conn, provider=pay, cd=FakeCD(), vendor_list=VENDORS)
    iid = s["invoices"][0]["invoice_id"]
    assert pipeline.decide(iid, "approved", "Priya", conn=conn) == "needs_second_approver"
    with pytest.raises(ValueError):
        pipeline.decide(iid, "approved", "Priya", conn=conn)  # same person again
    s = pipeline.process(rid, conn, provider=pay, cd=FakeCD(), vendor_list=VENDORS)
    assert s["invoices"][0]["state"] == "AWAITING_APPROVAL" and not pay.by_key
    assert pipeline.decide(iid, "approved", "Arjun", conn=conn) == "approved"
    s = pipeline.process(rid, conn, provider=pay, cd=FakeCD(), vendor_list=VENDORS)
    assert s["invoices"][0]["state"] == "PAID" and len(pay.by_key) == 1
