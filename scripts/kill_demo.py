"""Kill-on-stage, in one command (TEST DATA, separate database data/killdemo.db).

  .venv/bin/python scripts/kill_demo.py

1. one invoice from a cleared sample vendor is checked and approved;
2. the payout step runs and the process is KILLED right after the payout was created,
   before the desk recorded it (exit code 9);
3. the process restarts (`resume`): it finds the saved idempotency key and sends it again;
4. a second restart changes nothing.
It then shows there is exactly ONE payout. Payouts go to the local sandbox (simulated) unless
RazorpayX test keys are set.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "data", "killdemo.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)
vend = os.path.join(tempfile.mkdtemp(), "vendors.json")
json.dump([{"vendor_id": "V-DEMO", "legal_name": "Blue Dart Express Limited", "gstin": "27AAACB0446L1ZS",
            "domain": "bluedart.com", "status": "cleared", "verified_at": "2026-11-22",
            "verified_bank_account": {"ifsc": "HDFC0000001", "account_no": "50100012345678"}}], open(vend, "w"))
env = {**os.environ, "PAYDESK_DB": DB, "VENDORS_FILE": vend}
os.environ.update(PAYDESK_DB=DB, VENDORS_FILE=vend)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, ".env"))
from app import db, pipeline  # noqa: E402
db.DB_PATH = DB
conn = db.connect()
req = {"invoices": [{"invoice_no": "KILL-001", "vendor_name": "Blue Dart Express Ltd", "vendor_gstin": "27AAACB0446L1ZS",
                     "amount": "₹18,500", "invoice_date": "2026-10-20", "po_number": "PO-K1"}],
       "purchase_orders": [{"po_number": "PO-K1", "amount": 18500}]}
rid = pipeline.submit(req, conn)
s = pipeline.process(rid, conn, provider=None)
iid = s["invoices"][0]["invoice_id"]
print("1. checked:", s["invoices"][0]["outcome"]); pipeline.decide(iid, "approved", "demo approver", conn=conn)
print("   approved by a person")

def run(label, crash):
    e = dict(env, PAYDESK_CRASH_AFTER_PAYOUT="1" if crash else "0")
    r = subprocess.run([sys.executable, "-m", "app.cli", "resume"], cwd=ROOT, env=e, capture_output=True, text=True)
    c = db.connect()
    p = c.execute("SELECT idempotency_key, status, provider_ref FROM payments").fetchone()
    n = c.execute("SELECT COUNT(*) FROM sandbox_payouts").fetchone()[0] if c.execute(
        "SELECT name FROM sqlite_master WHERE name='sandbox_payouts'").fetchone() else 0
    st = c.execute("SELECT state FROM invoices WHERE invoice_id=?", (iid,)).fetchone()[0]
    print(f"{label}: exit={r.returncode} invoice={st} payment={dict(p) if p else None} payouts_at_provider={n}")
    return n

run("2. pay, then KILLED", crash=True)
run("3. restart (resume)", crash=False)
n = run("4. restart again", crash=False)
print("\nRESULT:", "exactly one payout" if n == 1 else f"{n} payouts (BUG)")
