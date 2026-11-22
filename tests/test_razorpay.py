"""RazorpayX test-mode payouts against a mocked HTTP API (tests only; no real calls)."""
import json
import os
import tempfile

import httpx
import pytest

from app import db, pipeline
from app.db import connect
from app.payments import ProviderNotConfigured, RazorpayXTest
from tests.test_pipeline import POS, VENDORS, FakeCD, inv


@pytest.fixture(autouse=True)
def env(monkeypatch):
    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_dummy")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setenv("RAZORPAYX_ACCOUNT_NUMBER", "2323230000000000")
    yield


class MockRazorpayX:
    """Behaves like RazorpayX: one payout per idempotency key; a retry returns the same payout."""

    def __init__(self, fail_first=0, reject=False, status="processing"):
        self.fail_first, self.reject, self.status = fail_first, reject, status
        self.by_key, self.requests = {}, []

    def handler(self, request: httpx.Request):
        self.requests.append(request)
        if request.method == "POST":
            if self.fail_first:
                self.fail_first -= 1
                raise httpx.ConnectTimeout("timed out", request=request)
            if self.reject:
                return httpx.Response(400, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "Invalid IFSC Code", "field": "ifsc"}})
            key = request.headers["X-Payout-Idempotency"]
            if key not in self.by_key:
                self.by_key[key] = {"id": f"pout_{len(self.by_key) + 1:014d}", "status": self.status, "utr": None,
                                    "body": json.loads(request.content)}
            p = self.by_key[key]
            return httpx.Response(200, json={"id": p["id"], "entity": "payout", "status": p["status"], "utr": p["utr"]})
        pid = request.url.path.rsplit("/", 1)[-1]
        p = next(v for v in self.by_key.values() if v["id"] == pid)
        return httpx.Response(200, json={"id": pid, "status": p["status"], "utr": p["utr"],
                                         "status_details": {"description": p.get("why"), "reason": p.get("reason")}})

    def provider(self):
        return RazorpayXTest(client=httpx.Client(transport=httpx.MockTransport(self.handler)))


def approve_and_pay(api, n="INV-1"):
    conn = connect()
    rid = pipeline.submit({"invoices": [inv(n, "₹18,500")], "purchase_orders": POS}, conn)
    s = pipeline.process(rid, conn, provider=api.provider(), cd=FakeCD(), vendor_list=VENDORS)
    iid = s["invoices"][0]["invoice_id"]
    pipeline.decide(iid, "approved", "AP lead", conn=conn)
    return rid, iid, conn, pipeline.process(rid, conn, provider=api.provider(), cd=FakeCD(), vendor_list=VENDORS)


def test_payout_request_is_well_formed():
    api = MockRazorpayX()
    _, iid, conn, s = approve_and_pay(api)
    assert s["invoices"][0]["state"] == "PAID"
    req = api.requests[-1]
    body = json.loads(req.content)
    assert req.headers["X-Payout-Idempotency"]
    assert body["amount"] == 1850000 and body["currency"] == "INR" and body["mode"] == "IMPS"
    assert body["purpose"] == "vendor bill" and body["account_number"] == "2323230000000000"
    assert body["fund_account"]["bank_account"]["account_number"] == "50100012345678"  # the VERIFIED account
    assert len(body["narration"]) <= 30 and len(body["reference_id"]) <= 40
    p = conn.execute("SELECT * FROM payments WHERE invoice_id=?", (iid,)).fetchone()
    assert p["provider_ref"] == "pout_00000000000001" and p["provider_status"] == "processing"


def test_timeout_then_resume_pays_once_with_same_key():
    api = MockRazorpayX(fail_first=1)
    rid, iid, conn, s = approve_and_pay(api)
    assert s["invoices"][0]["state"] == "PAYING"  # outcome unknown: not paid, not failed
    key1 = conn.execute("SELECT idempotency_key FROM payments WHERE invoice_id=?", (iid,)).fetchone()[0]
    s = pipeline.process(rid, connect(), provider=api.provider(), cd=FakeCD(), vendor_list=VENDORS)  # "restart"
    assert s["invoices"][0]["state"] == "PAID"
    sent_keys = {r.headers["X-Payout-Idempotency"] for r in api.requests if r.method == "POST"}
    assert sent_keys == {key1} and len(api.by_key) == 1


def test_rejected_payout_is_not_retried_and_goes_to_a_person():
    api = MockRazorpayX(reject=True)
    rid, iid, conn, s = approve_and_pay(api)
    assert s["invoices"][0]["state"] == "PAYOUT_FAILED"
    assert "Invalid IFSC" in s["invoices"][0]["reasons"][-1]["text"]
    n = len(api.requests)
    pipeline.process(rid, conn, provider=api.provider(), cd=FakeCD(), vendor_list=VENDORS)
    assert len(api.requests) == n  # terminal: no second attempt


def test_sync_marks_reversed_payout():
    api = MockRazorpayX()
    rid, iid, conn, s = approve_and_pay(api)
    p = next(iter(api.by_key.values()))
    p.update(status="reversed", why="Beneficiary account is closed", reason="beneficiary_bank_rejected")
    out = pipeline.sync_payouts(conn, api.provider())
    assert out[0]["status"] == "reversed"
    st = conn.execute("SELECT state FROM invoices WHERE invoice_id=?", (iid,)).fetchone()["state"]
    assert st == "PAYOUT_FAILED"


def test_live_key_refused(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_abc")
    with pytest.raises(ProviderNotConfigured):
        RazorpayXTest()
