"""Mock data lives ONLY here (tests). The live service reads real vendor records
and calls Crustdata and the payment provider in test mode."""
import os
import tempfile

import pytest

from app import db, pipeline  # noqa: E402
from app.db import connect  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")  # every test starts from an empty desk
    yield

VENDORS = [
    {"vendor_id": "V1", "legal_name": "Blue Dart Express Limited", "gstin": "27AAACB0446L1ZS",
     "domain": "bluedart.com", "crustdata_company_id": 736, "status": "cleared", "verified_at": "2026-11-22",
     "verified_bank_account": {"ifsc": "HDFC0000001", "account_no": "50100012345678"}},
    {"vendor_id": "V2", "legal_name": "Sharma Traders", "gstin": "", "domain": "", "status": "cannot_verify",
     "verified_bank_account": None},
]


class FakeCD:
    def __init__(self):
        self.calls = 0

    def identify_domain(self, d, inv_id=None):
        self.calls += 1
        ids = {"bluedart.com": (736, "Blue Dart"), "bluedart-logistics.in": (999, "Bluedart Logistics")}
        if d not in ids:
            return [], "2026-11-22T00:00:00+00:00"
        cid, name = ids[d]
        return [{"company_data": {"basic_info": {"crustdata_company_id": cid, "name": name, "primary_domain": d,
                                                 "all_domains": [d]}}}], "2026-11-22T00:00:00+00:00"


class FakePay:
    name = "fake_test"

    def __init__(self, crash_after_call=False):
        self.by_key = {}
        self.crash_after_call = crash_after_call

    def pay(self, key, amount, name, acct, ref):
        if key not in self.by_key:  # provider-side idempotency, like Razorpay
            self.by_key[key] = f"pout_{len(self.by_key) + 1}"
        if self.crash_after_call:
            self.crash_after_call = False
            raise KeyboardInterrupt("killed right after the provider accepted the payout")
        return self.by_key[key]


def inv(no, amount, date="2026-10-07", **kw):
    base = {"invoice_no": no, "vendor_name": "Blue Dart Express Ltd", "vendor_gstin": "27AAACB0446L1ZS",
            "vendor_domain": "bluedart.com", "amount": amount, "invoice_date": date, "po_number": "PO-1"}
    base.update(kw)
    return base


POS = [{"po_number": "PO-1", "amount": 18500}, {"po_number": "PO-2", "amount": 1240}]


def run(request, pay=None):
    conn = connect()
    rid = pipeline.submit(request, conn)
    return rid, conn, pipeline.process(rid, conn, provider=pay or FakePay(), cd=FakeCD(), vendor_list=VENDORS)


def outcome(s, line):
    return s["invoices"][line]


def test_duplicate_in_new_format_is_held():
    _, _, s = run({"invoices": [inv("INV-0042", "₹18,500"), inv("inv 42", 18500)], "purchase_orders": POS})
    assert outcome(s, 0)["outcome"] == "ready"
    assert outcome(s, 1)["outcome"] == "held"
    assert outcome(s, 1)["reasons"][-1]["code"] == "possible_duplicate"


def test_bank_detail_change_is_held():
    _, _, s = run({"invoices": [inv("INV-7", 18500, bank_account={"ifsc": "ICIC0000999", "account_no": "000111222333"})],
                   "purchase_orders": POS})
    assert outcome(s, 0)["reasons"][-1]["code"] == "bank_details_changed"


def test_lookalike_domain_is_held():
    _, _, s = run({"invoices": [inv("INV-8", 18500, vendor_domain="bluedart-logistics.in")], "purchase_orders": POS})
    assert outcome(s, 0)["reasons"][-1]["code"] == "lookalike_payee"


def test_unknown_payee_cannot_verify():
    _, _, s = run({"invoices": [inv("X1", 4000, vendor_name="PAYTM*SHARMA TRD", vendor_gstin="", vendor_domain="")]})
    assert outcome(s, 0)["outcome"] == "cannot_verify"


def test_uncleared_vendor_never_paid_even_if_approved():
    pay = FakePay()
    rid, conn, s = run({"invoices": [inv("S1", 4000, vendor_name="Sharma Traders", vendor_gstin="", vendor_domain="")]}, pay)
    assert outcome(s, 0)["reasons"][-1]["code"] == "vendor_not_cleared"
    pipeline.decide(outcome(s, 0)["invoice_id"], "approved", "tester", conn=conn)
    s = pipeline.process(rid, conn, provider=pay, cd=FakeCD(), vendor_list=VENDORS)
    assert outcome(s, 0)["outcome"] == "cannot_verify"
    assert pay.by_key == {}


def test_kill_after_payout_then_restart_pays_once():
    pay = FakePay(crash_after_call=True)
    rid, conn, s = run({"invoices": [inv("INV-100", 18500, date="2026-10-09")], "purchase_orders": POS}, pay)
    pipeline.decide(outcome(s, 0)["invoice_id"], "approved", "tester", conn=conn)
    with pytest.raises(KeyboardInterrupt):
        pipeline.process(rid, conn, provider=pay, cd=FakeCD(), vendor_list=VENDORS)
    # "restart": fresh connection, same database
    conn2 = connect()
    s = pipeline.process(rid, conn2, provider=pay, cd=FakeCD(), vendor_list=VENDORS)
    assert outcome(s, 0)["outcome"] == "paid"
    assert len(pay.by_key) == 1
    assert conn2.execute("SELECT COUNT(*) c FROM payments").fetchone()["c"] >= 1


def test_same_batch_twice_is_same_run():
    req = {"invoices": [inv("INV-555", 1240, po_number="PO-2", date="2026-10-03")], "purchase_orders": POS}
    r1, _, _ = run(req)
    r2, _, _ = run(req)
    assert r1 == r2
