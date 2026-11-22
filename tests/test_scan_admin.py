"""Bill upload/scan, supporting documents, verification checklist, administrator login."""
import os
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import auth, db, inbox, scan
from tests.test_pipeline import POS, VENDORS

BILL = b"""From: Blue Dart Express Limited <billing@bluedart.com>
Tax Invoice
Invoice No: BD/2026/0777
Invoice Date: 15/10/2026
GSTIN: 27AAACB0446L1ZS
PO Number: PO-1
Total Amount Payable: Rs. 18,500.00
"""


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    db.DB_PATH = str(tmp_path / "t.db")
    monkeypatch.setattr(inbox, "DOCS_DIR", str(tmp_path / "docs"))
    monkeypatch.setattr(scan, "DOCS_DIR", str(tmp_path / "docs"))
    monkeypatch.setattr(auth, "ADMIN_FILE", str(tmp_path / "admin.json"))
    monkeypatch.setattr(auth, "_failures", {})
    monkeypatch.setenv("PAYDESK_SECRET", "s")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    vend = tmp_path / "vendors.json"
    vend.write_text(__import__("json").dumps(VENDORS))
    monkeypatch.setenv("VENDORS_FILE", str(vend))
    pos = tmp_path / "pos.json"
    pos.write_text(__import__("json").dumps(POS))
    monkeypatch.setenv("PURCHASE_ORDERS_FILE", str(pos))
    yield


def signed_in():
    from app.server import app
    c = TestClient(app)
    assert c.post("/auth/local", json={"name": "Priya", "email": "priya@co.test"}).status_code == 200
    return c


def test_upload_text_bill_reads_checks_and_keeps_original(monkeypatch):
    from tests.test_pipeline import FakeCD
    import app.pipeline as pl
    monkeypatch.setattr(pl, "Crustdata", lambda *a, **k: FakeCD())
    c = signed_in()
    r = c.post("/bills/upload", files=[("files", ("bill.txt", BILL, "text/plain"))])
    assert r.status_code == 200, r.text
    inv = r.json()["run"]["invoices"][0]
    assert inv["outcome"] == "ready"
    doc = c.get(f"/invoices/{inv['invoice_id']}/doc").text
    assert "BD/2026/0777" in doc and "Payment verification checklist" in doc and "Uploaded by Priya" in doc


def test_attachment_turns_goods_receipt_check_green(monkeypatch):
    from tests.test_pipeline import FakeCD
    import app.pipeline as pl
    monkeypatch.setattr(pl, "Crustdata", lambda *a, **k: FakeCD())
    c = signed_in()
    iid = c.post("/bills/upload", files=[("files", ("bill.txt", BILL, "text/plain"))]).json()["run"]["invoices"][0]["invoice_id"]
    before = {x["check"]: x["result"] for x in c.get(f"/invoices/{iid}/checklist").json()}
    assert before["Goods receipt or delivery proof on file"] == "missing"
    r = c.post(f"/invoices/{iid}/attachments", data={"kind": "goods_receipt"}, files={"file": ("grn.txt", b"GRN 1 received", "text/plain")})
    assert r.status_code == 200
    after = {x["check"]: x["result"] for x in c.get(f"/invoices/{iid}/checklist").json()}
    assert after["Goods receipt or delivery proof on file"] == "pass"


def test_rejects_unsupported_files():
    c = signed_in()
    assert c.post("/bills/upload", files=[("files", ("x.exe", b"MZ", "application/octet-stream"))]).status_code == 400


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
def test_ocr_reads_text_from_an_image(tmp_path):
    # render a tiny text image with tesseract-compatible PNG via ImageMagick-free path: skip if PIL missing
    PIL = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont
    img = PIL.new("RGB", (1400, 160), "white")
    ImageDraw.Draw(img).text((20, 40), "Invoice No: BD/2026/0778", fill="black", font=ImageFont.load_default(size=56))
    p = tmp_path / "b.png"; img.save(p)
    assert "BD" in scan.ocr_image(str(p))


def test_admin_login_lockout_and_admin_only_page():
    auth.set_admin("admin", "correct-horse-battery")
    from app.server import app
    c = TestClient(app)
    for _ in range(5):
        assert c.post("/auth/admin", json={"username": "admin", "password": "nope"}).status_code == 401
    r = c.post("/auth/admin", json={"username": "admin", "password": "correct-horse-battery"})
    assert r.status_code == 401 and "Too many" in r.json()["detail"]
    auth._failures.clear()
    assert c.post("/auth/admin", json={"username": "admin", "password": "correct-horse-battery"}).status_code == 200
    assert c.get("/me").json()["role"] == "admin"
    assert c.get("/admin/overview").status_code == 200
    u = signed_in()
    assert u.get("/admin/overview").status_code == 403
