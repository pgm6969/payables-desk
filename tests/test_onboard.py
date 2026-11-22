"""Onboarding an unknown vendor, then re-checking its 'cannot verify' bill."""
import json

import pytest

from app import db, pipeline, trust, vendors
from app.db import connect
from tests.test_pipeline import FakeCD, FakePay


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    db.DB_PATH = str(tmp_path / "t.db")
    f = tmp_path / "vendors.json"
    f.write_text("[]")
    monkeypatch.setenv("VENDORS_FILE", str(f))
    yield


BILL = {"invoice_no": "HR0627WI012747", "vendor_name": "DTDC Express Limited", "vendor_gstin": "06AAACD8017H1ZW",
        "vendor_domain": "", "amount": "935.00", "invoice_date": "2026-07-25", "po_number": ""}


def test_unknown_vendor_then_onboarded_then_recheck_moves_ahead():
    conn = connect()
    rid = pipeline.submit({"invoices": [BILL]}, conn)
    s = pipeline.process(rid, conn, provider=FakePay(), cd=FakeCD())
    assert s["invoices"][0]["state"] == "CANNOT_VERIFY"
    vendors.save_vendor({"legal_name": "DTDC Express Limited", "gstin": "06AAACD8017H1ZW", "status": "cleared",
                         "verified_bank_account": {"ifsc": "HDFC0000001", "account_no": "123456789"}})
    out = pipeline.recheck(s["invoices"][0]["invoice_id"], "AP lead", "vendor onboarded", conn)
    inv = out["invoices"][0]
    assert inv["state"] == "AWAITING_APPROVAL"                      # moved ahead, now a person decides
    assert any(r["code"] == "rechecked" for r in inv["reasons"])     # history kept
    assert inv["reasons"][-1]["code"] == "po_mismatch"              # the bill has no PO: held with that reason


def test_gst_registry_evidence_flags_a_cancelled_gstin():
    class API:
        conn = connect()

        def post(self, endpoint, body, inv=None):
            return [{"matches": []}], "2026-11-22"
    rep = trust.check_vendor(API(), {"legal_name": "X Ltd", "domain": "", "gst_registry": {"status": "Cancelled", "legal_name": "X Ltd"}})
    assert rep["verdict"] == "CANNOT_VERIFY"  # no domain -> cannot resolve; registry facts never override that
