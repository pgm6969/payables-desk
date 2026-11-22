"""Inbox reader tests. The model is faked here (tests only); the checks it feeds are real."""
import json
import os
import tempfile
from types import SimpleNamespace

import pytest

from app import db, inbox, pipeline
from app.cli import run_inbox
from app.db import connect
from tests.test_pipeline import POS, VENDORS, FakeCD, FakePay


@pytest.fixture(autouse=True)
def fresh_db():
    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    yield


def ev(**kw):
    return [{"field": k, "quote": v} for k, v in kw.items() if v]


INVOICE_MAIL = {
    "kind": "invoice",
    "invoices": [{
        "invoice_no": "BD/2026/0042", "vendor_name": "Blue Dart Express Ltd", "vendor_gstin": "27AAACB0446L1ZS",
        "vendor_domain": "", "amount": "₹18,500.00", "currency": "INR", "invoice_date": "2026-10-07",
        "po_number": "PO-1", "bank_ifsc": "", "bank_account_no": "",
        "evidence": ev(invoice_no="Invoice No: BD/2026/0042", amount="Total payable: ₹18,500.00"),
    }],
    "bank_change": {"vendor_name": "", "vendor_gstin": "", "vendor_domain": "", "new_ifsc": "", "new_account_no": "", "quote": ""},
}
BANK_MAIL = {
    "kind": "bank_change_request", "invoices": [],
    "bank_change": {"vendor_name": "Blue Dart Express Limited", "vendor_gstin": "27AAACB0446L1ZS", "vendor_domain": "",
                    "new_ifsc": "ICIC0009999", "new_account_no": "123456789012",
                    "quote": "kindly update our bank details to ICICI A/c 123456789012"},
}


class FakeClient:
    """Mimics client.beta.messages.create(...) closely enough for inbox.extract."""

    def __init__(self, by_subject):
        self.by_subject = by_subject
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls += 1
        text = kw["messages"][0]["content"][-1]["text"]
        payload = next(v for k, v in self.by_subject.items() if k in text)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            usage=SimpleNamespace(input_tokens=1200, output_tokens=300),
            stop_reason="end_turn", model="claude-opus-5-5")


def make_inbox():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "01_invoice.json"), "w") as f:
        json.dump({"from": "Accounts <billing@bluedart.com>", "subject": "Invoice BD/2026/0042",
                   "date": "2026-10-08", "body": "Please find invoice..."}, f)
    with open(os.path.join(d, "02_bank.txt"), "w") as f:
        f.write("From: Blue Dart Accounts <accounts@bluedart-payments.co>\nSubject: Updated bank details\n\nkindly update our bank details")
    with open(os.path.join(d, "purchase_orders.json"), "w") as f:
        json.dump(POS, f)
    return d


def test_inbox_invoice_is_read_with_evidence_and_bank_change_is_held():
    client = FakeClient({"Invoice BD/2026/0042": INVOICE_MAIL, "Updated bank details": BANK_MAIL})
    conn = connect()
    out = run_inbox(make_inbox(), conn, client=client, provider=FakePay(), cd=FakeCD(), vendor_list=VENDORS)
    assert len(out["invoices"]) == 1
    inv = out["invoices"][0]
    assert inv["outcome"] == "held"
    assert inv["reasons"][-1]["code"] == "bank_change_requested"
    quotes = [r["value"] for r in conn.execute("SELECT value FROM sources WHERE invoice_id=?", (inv["invoice_id"],))]
    assert "Total payable: ₹18,500.00" in quotes
    cost = conn.execute("SELECT SUM(inr) c FROM ledger WHERE service='model' AND run_id=?", (out["run_id"],)).fetchone()["c"]
    assert cost > 0


def test_inbox_reread_uses_cache_not_model():
    client = FakeClient({"Invoice BD/2026/0042": INVOICE_MAIL, "Updated bank details": BANK_MAIL})
    conn = connect()
    folder = make_inbox()
    r1 = run_inbox(folder, conn, client=client, provider=FakePay(), cd=FakeCD(), vendor_list=VENDORS)
    r2 = run_inbox(folder, conn, client=client, provider=FakePay(), cd=FakeCD(), vendor_list=VENDORS)
    assert client.calls == 2           # two messages, read once each
    assert r1["run_id"] == r2["run_id"]  # same inbox -> same run, no double work


def test_clean_invoice_without_bank_mail_is_ready():
    client = FakeClient({"Invoice BD/2026/0042": INVOICE_MAIL})
    d = make_inbox()
    os.remove(os.path.join(d, "02_bank.txt"))
    out = run_inbox(d, connect(), client=client, provider=FakePay(), cd=FakeCD(), vendor_list=VENDORS)
    assert out["invoices"][0]["outcome"] == "ready"
