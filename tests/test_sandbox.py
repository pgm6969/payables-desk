"""The local sandbox (simulated payouts) must keep the exactly-once contract."""
import os
import tempfile

import pytest

from app import db, pipeline
from app.db import connect
from app.payments import LocalSandbox, PayoutRejected, get_provider
from tests.test_pipeline import POS, VENDORS, FakeCD, inv


@pytest.fixture(autouse=True)
def env(monkeypatch):
    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    for k in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAYX_ACCOUNT_NUMBER", "PAYDESK_PAYMENTS"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_without_keys_the_sandbox_is_used_and_labelled():
    p = get_provider(connect())
    assert isinstance(p, LocalSandbox) and p.simulated and p.name == "local_sandbox"


def test_same_key_returns_same_payout_even_after_restart():
    acct = {"ifsc": "HDFC0000001", "account_no": "50100012345678"}
    a = LocalSandbox(connect()).pay("key-0001", 1850000, "Blue Dart", acct, "r1")
    b = LocalSandbox(connect()).pay("key-0001", 1850000, "Blue Dart", acct, "r1")  # new process, same DB
    assert a["id"] == b["id"]
    with pytest.raises(PayoutRejected):
        LocalSandbox(connect()).pay("key-0001", 999, "Blue Dart", acct, "r1")  # same key, different body


def test_kill_after_payout_restart_pays_once_end_to_end():
    conn = connect()
    rid = pipeline.submit({"invoices": [inv("INV-1", "₹18,500")], "purchase_orders": POS}, conn)
    s = pipeline.process(rid, conn, cd=FakeCD(), vendor_list=VENDORS)
    iid = s["invoices"][0]["invoice_id"]
    pipeline.decide(iid, "approved", "AP lead", conn=conn)

    class KilledAfterCall(LocalSandbox):
        def pay(self, *a, **k):
            super().pay(*a, **k)
            raise KeyboardInterrupt("process killed after the payout was created")

    with pytest.raises(KeyboardInterrupt):
        pipeline.process(rid, conn, provider=KilledAfterCall(conn), cd=FakeCD(), vendor_list=VENDORS)
    s = pipeline.process(rid, connect(), cd=FakeCD(), vendor_list=VENDORS)  # restart, default provider = sandbox
    assert s["invoices"][0]["state"] == "PAID"
    assert connect().execute("SELECT COUNT(*) FROM sandbox_payouts").fetchone()[0] == 1


def test_fail_ifsc_is_rejected_like_razorpay():
    with pytest.raises(PayoutRejected):
        LocalSandbox(connect()).pay("key-0002", 100, "X Vendor", {"ifsc": "FAIL0000001", "account_no": "123456"}, "r2")
