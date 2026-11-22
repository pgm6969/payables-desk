"""Inbox reader without a model key: fixed patterns, evidence = the exact line."""
import json
import os
import tempfile

import pytest

from app import db, inbox
from app.cli import run_inbox
from app.db import connect
from tests.test_pipeline import POS, VENDORS, FakeCD, FakePay

INVOICE = """From: Blue Dart Express Limited
Tax Invoice
Invoice No: BD/2026/0099
Invoice Date: 12/10/2026
GSTIN: 27AAACB0446L1ZS
PO Number: PO-1
Freight charges, October
Total Amount Payable: Rs. 18,500.00
"""
BANK = """From: Blue Dart Express Limited <accounts@bluedart-pay.co>
Dear Accounts team,
Please note we have changed our bank details. Kindly pay all future invoices to
A/c No: 998877665544, IFSC: YESB0000123
Regards, Blue Dart Express Limited
"""


@pytest.fixture(autouse=True)
def env(monkeypatch):
    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield


def test_rule_reader_extracts_fields_with_their_lines():
    d = inbox.rule_extract({"subject": "", "body": INVOICE, "from": "", "pdfs": []})
    i = d["invoices"][0]
    assert (i["invoice_no"], i["vendor_gstin"], i["po_number"], i["invoice_date"]) == ("BD/2026/0099", "27AAACB0446L1ZS", "PO-1", "2026-10-12")
    assert i["amount"].endswith("18,500.00")
    assert {"field": "amount", "quote": "Total Amount Payable: Rs. 18,500.00"} in i["evidence"]


def test_rule_reader_flags_bank_change_email():
    d = inbox.rule_extract({"subject": "Updated bank details", "body": BANK, "from": "accounts@bluedart-pay.co", "pdfs": []})
    assert d["kind"] == "bank_change_request" and d["bank_change"]["new_account_no"] == "998877665544"


def test_inbox_without_model_key_end_to_end():
    folder = tempfile.mkdtemp()
    open(os.path.join(folder, "01_invoice.txt"), "w").write(INVOICE)
    open(os.path.join(folder, "02_bank.txt"), "w").write(BANK)
    json.dump(POS, open(os.path.join(folder, "purchase_orders.json"), "w"))
    out = run_inbox(folder, connect(), provider=FakePay(), cd=FakeCD(), vendor_list=VENDORS)
    assert len(out["invoices"]) == 1
    assert out["invoices"][0]["reasons"][-1]["code"] in ("bank_change_requested",)
