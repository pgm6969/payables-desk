"""Bank-statement posting: the organizers' example case, with Crustdata faked (tests only)."""
import os
import tempfile

import pytest

from app import bank, db
from app.db import connect


@pytest.fixture(autouse=True)
def fresh_db():
    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    yield


def co(name, domain, size, desc="", conf=1.0):
    return {"confidence_score": conf, "company_data": {"basic_info": {"name": name, "primary_domain": domain,
                                                                       "employee_count_range": size, "description": desc}}}


class FakeCD:
    def __init__(self):
        self.conn = connect()

    def post(self, endpoint, body, inv=None):
        n = body["names"][0].lower()
        table = {
            "swiggy": [co("Swiggy", "swiggy.com", "10001+", "India's on-demand platform for food and groceries"),
                       co("swiggy", None, None), co("Swiggy", None, "11-50")],
            "sharma traders": [co("Sharma Traders", "sharmatraders.co", "11-50"), co("sharma Traders", None, "myself only"),
                               co("Sharma Traders", None, "2-10")],
        }
        return [{"matches": table.get(n, [])}], "2026-11-22T10:00:00+00:00"


VENDORS = [{"vendor_id": "V-AWS", "legal_name": "Amazon Web Services EMEA SARL", "bank_descriptors": ["AWS EMEA"],
            "default_account": "Cloud hosting"}]

LINES = {"bank_lines": [
    {"date": "3 Oct 2026", "description": "RAZ*SWIGGY BLR", "amount": "₹1,240"},
    {"date": "7 Oct 2026", "description": "AWS EMEA", "amount": "₹18,500"},
    {"date": "7 Oct 2026", "description": "AWS EMEA", "amount": "₹18,500"},
    {"date": "9 Oct 2026", "description": "PAYTM*SHARMA TRD", "amount": "₹4,000"},
]}


def test_organizers_example_case():
    conn = connect()
    b = bank.submit(LINES, conn)
    s = bank.process(b, conn, cd=FakeCD(), vendor_list=VENDORS)
    l = s["lines"]
    assert (l[0]["status"], l[0]["payee"], l[0]["account"]) == ("posted", "Swiggy", "Staff meals")
    assert (l[1]["status"], l[1]["payee"], l[1]["account"]) == ("posted", "Amazon Web Services EMEA SARL", "Cloud hosting")
    assert l[2]["status"] == "held" and l[2]["reasons"][-1]["code"] == "possible_duplicate"
    assert l[3]["status"] == "cannot_verify" and l[3]["payee"] is None


def test_descriptor_cleaning():
    assert bank.clean_descriptor("RAZ*SWIGGY BLR") == "SWIGGY"
    assert bank.clean_descriptor("PAYTM*SHARMA TRD") == "SHARMA TRADERS"
    assert bank.clean_descriptor("UPI/AWS EMEA/12345") == "AWS"


def test_same_batch_twice_is_not_posted_twice():
    conn = connect()
    b1 = bank.submit(LINES, conn); bank.process(b1, conn, cd=FakeCD(), vendor_list=VENDORS)
    b2 = bank.submit(LINES, conn)
    assert b1 == b2
    assert conn.execute("SELECT COUNT(*) FROM bank_lines WHERE status='posted'").fetchone()[0] == 2
