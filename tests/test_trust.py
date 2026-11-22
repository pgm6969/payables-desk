"""Vendor check (5.1 + 5.2) and the extra payables checks. Crustdata is faked here (tests only)."""
import os
import tempfile

import pytest

from app import checks, db, pipeline, trust
from app.db import connect
from tests.test_pipeline import POS, VENDORS, FakeCD, FakePay, inv


@pytest.fixture(autouse=True)
def fresh_db():
    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    yield


class FakeAPI:
    """Answers the four Crustdata endpoints the vendor check uses."""

    def __init__(self, domains, news=(), director_news=(), site_title="Shadowfax Technologies - logistics"):
        self.conn = connect()
        self.domains, self.news, self.director_news, self.site_title = domains, news, director_news, site_title
        self.calls = []

    def post(self, endpoint, body, invoice_id=None):
        self.calls.append(endpoint)
        at = "2026-11-22T10:00:00+00:00"
        if endpoint == "/company/identify":
            d = (body.get("domains") or [""])[0]
            m = self.domains.get(d)
            return [{"matches": [{"company_data": {"basic_info": {"crustdata_company_id": m["id"], "name": m["name"],
                                                                   "primary_domain": d, "all_domains": [d]}}}] if m else []}], at
        if endpoint == "/company/enrich":
            m = next(v for v in self.domains.values() if v["id"] == body["crustdata_company_ids"][0])
            return [{"matches": [{"company_data": {
                "basic_info": {"name": m["name"], "primary_domain": m["domain"], "year_founded": 2015, "status": "active"},
                "headcount": {"total": 4000}, "locations": {"headquarters": "Bengaluru"},
                "people": {"founders": [{"basic_profile": {"name": "Abhishek Bansal", "current_title": "Co-founder & CEO"}}]}}}]}], at
        if endpoint == "/web/enrich/live":
            return [{"success": True, "url": body["urls"][0], "http_status": 200, "title": self.site_title, "content": ""}], at
        if endpoint == "/web/search/live":
            is_director = "Abhishek" in body["query"] or "Kumar" in body["query"]
            return {"results": list(self.director_news if is_director else self.news)}, at
        raise AssertionError(endpoint)


SFX = {"shadowfax.in": {"id": 42, "name": "Shadowfax Technologies", "domain": "shadowfax.in"}}


def test_real_vendor_with_director_is_clear():
    api = FakeAPI(SFX)
    rep = trust.check_vendor(api, {"legal_name": "Shadowfax Technologies Pvt Ltd", "domain": "shadowfax.in", "director": "Abhishek Bansal"})
    assert rep["verdict"] == "CLEAR"
    assert any("Co-founder" in f["fact"] for f in rep["facts"])


def test_front_company_without_domain_record_cannot_verify():
    rep = trust.check_vendor(FakeAPI({}), {"legal_name": "Shreeji Cargo Movers", "domain": "shreejicargo.in"})
    assert rep["verdict"] == "CANNOT_VERIFY"
    assert "owns the domain" in rep["cannot_verify"][0]


def test_director_namesakes_cleared_but_real_hit_escalated():
    namesake = {"title": "Abhishek Bansal arrested in cheating case in Jaipur", "snippet": "local trader", "url": "https://n/1"}
    real = {"title": "Shadowfax Technologies CEO Abhishek Bansal named in FIR", "snippet": "", "url": "https://www.google.com/goto?url=https://n/2"}
    rep = trust.check_vendor(FakeAPI(SFX, director_news=[namesake]),
                             {"legal_name": "Shadowfax Technologies", "domain": "shadowfax.in", "director": "Abhishek Bansal"})
    assert rep["verdict"] == "CLEAR" and rep["screening"]["hits_cleared"] == 1
    rep = trust.check_vendor(FakeAPI(SFX, director_news=[namesake, real]),
                             {"legal_name": "Shadowfax Technologies", "domain": "shadowfax.in", "director": "Abhishek Bansal"})
    assert rep["verdict"] == "HOLD" and rep["screening"]["hits_escalated"] == 1
    assert rep["flags"][0]["url"] == "https://n/2"  # cites the article, not the redirect


def test_director_not_on_record_cannot_verify():
    rep = trust.check_vendor(FakeAPI(SFX), {"legal_name": "Shadowfax Technologies", "domain": "shadowfax.in", "director": "Ravi Kumar"})
    assert rep["verdict"] == "CANNOT_VERIFY"


def test_victim_news_does_not_flag_company():
    scam = {"title": "Fraudsters posing as Shadowfax delivery agents cheat residents", "snippet": "fraud", "url": "https://n/3"}
    rep = trust.check_vendor(FakeAPI(SFX, news=[scam]), {"legal_name": "Shadowfax Technologies", "domain": "shadowfax.in"})
    assert rep["verdict"] == "CLEAR"


def test_gstin_check_digit_and_entity_type():
    assert checks.gstin_problems("27AAACR5055K1Z7", "Reliance Industries Limited") == []
    assert checks.gstin_problems("27AAACR5055K1Z8", "X Ltd")[0][0] == "gstin_check_digit"
    g = "27AAAPR5055K1Z" + checks.gstin_check_char("27AAAPR5055K1Z")  # PAN of an individual
    assert checks.gstin_problems(g, "Some Logistics Private Limited")[0][0] == "gstin_entity_type"


def run(request, pay=None, vendors=VENDORS):
    conn = connect()
    rid = pipeline.submit(request, conn)
    return rid, conn, pipeline.process(rid, conn, provider=pay or FakePay(), cd=FakeCD(), vendor_list=vendors)


def test_lookalike_sender_domain_held_without_lookup():
    _, _, s = run({"invoices": [inv("INV-9", 18500, vendor_domain="bluedartt.com")], "purchase_orders": POS})
    assert s["invoices"][0]["reasons"][-1]["code"] == "lookalike_sender"


def test_po_cannot_be_billed_twice_under_different_amounts():
    _, _, s = run({"invoices": [inv("A-1", 18500, date="2026-10-01"), inv("A-2", 9000, date="2026-10-02")],
                   "purchase_orders": POS})
    assert s["invoices"][0]["outcome"] == "ready"
    codes = [r["code"] for r in s["invoices"][1]["reasons"]]
    assert "po_already_billed" in codes or "po_mismatch" in codes


def test_bank_change_creates_draft_to_contact_on_file():
    vendors = [dict(VENDORS[0], contact={"name": "Asha", "email": "ap@bluedart.com", "phone": "+91-22-0000"}), VENDORS[1]]
    rid, conn, s = run({"invoices": [inv("INV-7", 18500, bank_account={"ifsc": "ICIC0000999", "account_no": "000111222333"})],
                        "purchase_orders": POS}, vendors=vendors)
    d = conn.execute("SELECT * FROM drafts WHERE invoice_id=?", (s["invoices"][0]["invoice_id"],)).fetchone()
    assert d["to_email"] == "ap@bluedart.com"


def test_vendor_changed_after_approval_is_held_again():
    pay = FakePay()
    rid, conn, s = run({"invoices": [inv("INV-55", 18500)], "purchase_orders": POS}, pay)
    iid = s["invoices"][0]["invoice_id"]
    pipeline.decide(iid, "approved", "tester", conn=conn)
    # approval is recorded, then the vendor master changes before the payment step runs
    pipeline.step_approval(conn, iid, VENDORS[0])
    changed = [dict(VENDORS[0], verified_bank_account={"ifsc": "HDFC0000001", "account_no": "999"}), VENDORS[1]]
    s = pipeline.process(rid, conn, provider=pay, cd=FakeCD(), vendor_list=changed)
    assert s["invoices"][0]["reasons"][-1]["code"] == "vendor_changed"
    assert pay.by_key == {}
